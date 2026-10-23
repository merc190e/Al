// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import "@openzeppelin/contracts/access/Ownable.sol";

interface IUniswapV2Router02 {
    function factory() external pure returns (address);
    function WETH() external pure returns (address);
    function addLiquidityETH(
        address token,
        uint amountTokenDesired,
        uint amountTokenMin,
        uint amountETHMin,
        address to,
        uint deadline
    ) external payable returns (uint amountToken, uint amountETH, uint liquidity);
    function swapExactTokensForETHSupportingFeeOnTransferTokens(
        uint amountIn,
        uint amountOutMin,
        address[] calldata path,
        address to,
        uint deadline
    ) external;
}

interface IUniswapV2Factory {
    function createPair(address tokenA, address tokenB) external returns (address pair);
}

contract AMLToken is ERC20, Ownable {
    // Maksymalny podatek łączny (4.00% = 400 BPS)
    uint256 public constant MAX_TAX_BPS = 400; 
    
    // Domyślny podatek: 1% dla twórcy, 2% do płynności (3% łącznie)
    uint256 public creatorFeeBps = 100;   // 1.00%
    uint256 public liquidityFeeBps = 200; // 2.00%
    
    address public creatorWallet;
    address public uniswapV2Pair;
    IUniswapV2Router02 public uniswapV2Router;
    
    mapping(address => bool) private _isExcludedFromFee;
    mapping(address => bool) public isAutomatedMarketMakerPair;
    
    bool private inSwapAndLiquify;
    bool public swapAndLiquifyEnabled = true;
    uint256 public numTokensSellToEarnFees;

    event TaxUpdated(uint256 creatorFeeBps, uint256 liquidityFeeBps);
    event ExcludedFromFee(address account, bool isExcluded);
    event AutomatedMarketMakerPairSet(address pair, bool value);

    modifier lockTheSwap {
        inSwapAndLiquify = true;
        _;
        inSwapAndLiquify = false;
    }

    constructor(address _routerAddress) ERC20("AML", "AL") Ownable(msg.sender) {
        creatorWallet = msg.sender;
        
        // Wybicie 1,000,000 tokenów do portfela twórcy
        uint256 totalTokens = 1000000 * 10 ** decimals();
        _mint(msg.sender, totalTokens);
        
        numTokensSellToEarnFees = (totalTokens * 5) / 10000; // Próg wymiany opłat (0.05% podaży)
        
        IUniswapV2Router02 _router = IUniswapV2Router02(_routerAddress);
        address _pair = IUniswapV2Factory(_router.factory()).createPair(address(this), _router.WETH());
        
        uniswapV2Router = _router;
        uniswapV2Pair = _pair;
        
        _setAutomatedMarketMakerPair(_pair, true);
        
        // Zwolnienie twórcy i kontraktu z opłat
        _isExcludedFromFee[msg.sender] = true;
        _isExcludedFromFee[address(this)] = true;
    }

    /**
     * @notice Zmiana opłat. Łączny podatek nie może przekroczyć 4% (400 BPS).
     */
    function setTaxRates(uint256 _creatorFeeBps, uint256 _liquidityFeeBps) external onlyOwner {
        require(_creatorFeeBps + _liquidityFeeBps <= MAX_TAX_BPS, "Total tax exceeds maximum 4%");
        creatorFeeBps = _creatorFeeBps;
        liquidityFeeBps = _liquidityFeeBps;
        emit TaxUpdated(_creatorFeeBps, _liquidityFeeBps);
    }

    function setCreatorWallet(address _creatorWallet) external onlyOwner {
        require(_creatorWallet != address(0), "Invalid address");
        creatorWallet = _creatorWallet;
    }

    function setExcludedFromFee(address account, bool excluded) external onlyOwner {
        _isExcludedFromFee[account] = excluded;
        emit ExcludedFromFee(account, excluded);
    }

    function setAutomatedMarketMakerPair(address pair, bool value) external onlyOwner {
        require(pair != uniswapV2Pair, "Cannot remove primary pair");
        _setAutomatedMarketMakerPair(pair, value);
    }

    function _setAutomatedMarketMakerPair(address pair, bool value) private {
        isAutomatedMarketMakerPair[pair] = value;
        emit AutomatedMarketMakerPairSet(pair, value);
    }

    function _update(address from, address to, uint256 amount) internal override {
        if (amount == 0) {
            super._update(from, to, 0);
            return;
        }

        bool takeFee = true;
        if (_isExcludedFromFee[from] || _isExcludedFromFee[to]) {
            takeFee = false;
        }

        uint256 contractTokenBalance = balanceOf(address(this));
        bool canSwap = contractTokenBalance >= numTokensSellToEarnFees;

        if (
            canSwap &&
            !inSwapAndLiquify &&
            !isAutomatedMarketMakerPair[from] &&
            swapAndLiquifyEnabled &&
            takeFee
        ) {
            swapAndLiquify(contractTokenBalance);
        }

        uint256 fees = 0;
        if (takeFee) {
            if (isAutomatedMarketMakerPair[from] || isAutomatedMarketMakerPair[to]) {
                uint256 totalTax = creatorFeeBps + liquidityFeeBps;
                if (totalTax > 0) {
                    fees = (amount * totalTax) / 10000;
                }
            }
        }

        if (fees > 0) {
            super._update(from, address(this), fees);
            amount -= fees;
        }

        super._update(from, to, amount);
    }

    function swapAndLiquify(uint256 contractTokenBalance) private lockTheSwap {
        uint256 totalTax = creatorFeeBps + liquidityFeeBps;
        if (totalTax == 0) return;

        uint256 liquidityTokens = (contractTokenBalance * liquidityFeeBps) / totalTax;
        uint256 creatorTokens = contractTokenBalance - liquidityTokens;

        uint256 halfLiquidity = liquidityTokens / 2;
        uint256 otherHalfLiquidity = liquidityTokens - halfLiquidity;

        uint256 initialETHBalance = address(this).balance;
        uint256 tokensToSwap = creatorTokens + halfLiquidity;

        swapTokensForEth(tokensToSwap);

        uint256 newETHBalance = address(this).balance - initialETHBalance;
        
        uint256 ethForCreator = (newETHBalance * creatorTokens) / tokensToSwap;
        uint256 ethForLiquidity = newETHBalance - ethForCreator;

        if (ethForCreator > 0) {
            payable(creatorWallet).transfer(ethForCreator);
        }

        if (otherHalfLiquidity > 0 && ethForLiquidity > 0) {
            addLiquidity(otherHalfLiquidity, ethForLiquidity);
        }
    }

    function swapTokensForEth(uint256 tokenAmount) private {
        address[] memory path = new address[](2);
        path[0] = address(this);
        path[1] = uniswapV2Router.WETH();

        _approve(address(this), address(uniswapV2Router), tokenAmount);

        uniswapV2Router.swapExactTokensForETHSupportingFeeOnTransferTokens(
            tokenAmount,
            0,
            path,
            address(this),
            block.timestamp
        );
    }

    function addLiquidity(uint256 tokenAmount, uint256 ethAmount) private {
        _approve(address(this), address(uniswapV2Router), tokenAmount);

        uniswapV2Router.addLiquidityETH{value: ethAmount}(
            address(this),
            tokenAmount,
            0,
            0,
            owner(),
            block.timestamp
        );
    }

    receive() external payable {}
}