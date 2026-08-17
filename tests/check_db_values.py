import sqlite3

conn = sqlite3.connect('./data/renewise.db')
cur = conn.cursor()

print("--- GROUPS ---")
for row in cur.execute("SELECT id, price FROM groups LIMIT 2"):
    print(row)

print("\n--- SUBSCRIPTIONS ---")
for row in cur.execute("SELECT id, price_locked_in FROM subscriptions LIMIT 2"):
    print(row)

print("\n--- PROCESSED TX HASHES JOIN SUBSCRIPTIONS ---")
for row in cur.execute('''
    SELECT p.tx_hash, s.price_locked_in 
    FROM processed_tx_hashes p
    JOIN subscriptions s ON s.id = p.sub_id
    LIMIT 2
'''):
    print(row)

conn.close()
