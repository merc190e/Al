const hre = require("hardhat");

async function main() {
  // Wklej właściwy adres Routera DEX w sieci Robinhood Chain
  const routerAddress = "0x8876789976decbfcbbbe364623c63652db8c0904"; 

  const [deployer] = await hre.ethers.getSigners();
  if (!deployer) {
    throw new Error(
      `No deployer account is configured for the "${hre.network.name}" network. ` +
      "Set PRIVATE_KEY in .env to a valid 32-byte hex private key (with or without the 0x prefix)."
    );
  }

  const AMLToken = await hre.ethers.getContractFactory("AMLToken", deployer);
  const token = await AMLToken.deploy(routerAddress);

  await token.waitForDeployment();
  console.log(`AML Token deployed to: ${await token.getAddress()}`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});