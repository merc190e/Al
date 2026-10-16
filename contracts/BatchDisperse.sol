// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IERC20 {
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
}

/// Batch/Disperse: one transaction funds many sub-wallets (21 000 base gas paid once).
contract BatchDisperse {
    error LengthMismatch();
    error ValueMismatch();
    error TransferFailed();

    function disperseEth(address payable[] calldata to, uint256[] calldata amounts) external payable {
        uint256 n = to.length;
        if (n != amounts.length) revert LengthMismatch();
        uint256 total;
        for (uint256 i; i < n; ) {
            total += amounts[i];
            (bool ok, ) = to[i].call{value: amounts[i]}("");
            if (!ok) revert TransferFailed();
            unchecked { ++i; }
        }
        if (msg.value < total) revert ValueMismatch();
        if (msg.value > total) {
            (bool ok, ) = msg.sender.call{value: msg.value - total}("");
            if (!ok) revert TransferFailed();
        }
    }

    /// Requires prior approve(this, total) by the caller.
    function disperseToken(IERC20 token, address[] calldata to, uint256[] calldata amounts) external {
        uint256 n = to.length;
        if (n != amounts.length) revert LengthMismatch();
        for (uint256 i; i < n; ) {
            if (!token.transferFrom(msg.sender, to[i], amounts[i])) revert TransferFailed();
            unchecked { ++i; }
        }
    }
}
