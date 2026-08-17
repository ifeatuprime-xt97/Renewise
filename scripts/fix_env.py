import os
import asyncio
from pytoniq_core.crypto.keys import mnemonic_new
from pytoniq import WalletV4R2, LiteBalancer

async def generate_wallet():
    m = mnemonic_new(24)
    
    print("Connecting to Testnet to generate wallet address...")
    provider = LiteBalancer.from_testnet_config(trust_level=2)
    await provider.start_up()
    
    try:
        wallet = await WalletV4R2.from_mnemonic(provider=provider, mnemonics=m)
        
        print("\n--- NEW WALLET ---")
        print("Address:", wallet.address.to_str(is_url_safe=True, is_bounceable=True, is_test_only=True))
        print("Mnemonic:", " ".join(m))
        print("------------------\n")
        return m
    finally:
        await provider.close_all()

def main():
    m = asyncio.run(generate_wallet())
    
    env_file = ".env"
    with open(env_file, "r") as f:
        content = f.read()
    
    # Replace the old 12-word mnemonic with the new 24-word one
    old_line1 = "DEPLOYER_MNEMONIC= okay napkin ethics kiss guilt surface legend rubber execute skate ecology future"
    old_line2 = "DEPLOYER_MNEMONIC=okay napkin ethics kiss guilt surface legend rubber execute skate ecology future"
    new_line = "DEPLOYER_MNEMONIC=" + " ".join(m)
    
    if old_line1 in content:
        content = content.replace(old_line1, new_line)
    elif old_line2 in content:
        content = content.replace(old_line2, new_line)
        
    with open(env_file, "w") as f:
        f.write(content)
        
    print("Successfully updated .env file with new mnemonic!")

if __name__ == "__main__":
    main()
