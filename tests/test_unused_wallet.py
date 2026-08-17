import asyncio
import os
import sys

# Ensure project root is in python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from pytoniq_core import Address
from renewise.services.wallet import validate_ton_address

async def main():
    random_hash = os.urandom(32)
    addr = Address((0, random_hash))
    friendly_addr = addr.to_str(is_user_friendly=True, is_url_safe=True, is_bounceable=True)
    print(f"Testing fresh unused address: {friendly_addr}")
    
    result = await validate_ton_address(friendly_addr)
    print(f"Result: {result}")

if __name__ == "__main__":
    asyncio.run(main())
