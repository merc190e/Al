require("@nomicfoundation/hardhat-toolbox");
require("dotenv").config();

const privateKey = process.env.PRIVATE_KEY?.trim();
const normalizedPrivateKey = privateKey && /^[a-fA-F0-9]{64}$/.test(privateKey)
  ? `0x${privateKey}`
  : privateKey;
const accounts = normalizedPrivateKey && /^0x[a-fA-F0-9]{64}$/.test(normalizedPrivateKey)
  ? [normalizedPrivateKey]
  : [];

/** @type import('hardhat/config').HardhatUserConfig */
module.exports = {
  solidity: "0.8.20",
  paths: {
    sources: "./Contrakt",
  },
  networks: {
    robinhood: {
      url: process.env.ROBINHOOD_RPC_URL || "",
      accounts,
    },
  },
};