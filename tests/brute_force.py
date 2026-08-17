import sys

target = 3807777778
buyer_bps = 200 # global default? Let's check

for price in range(3730000000, 3740000000):
    val = price + price * buyer_bps // 10000
    if val == target:
        print(f"FOUND price for buyer_bps=200: {price}")
        break

buyer_bps = 289 # maybe a weird value?

