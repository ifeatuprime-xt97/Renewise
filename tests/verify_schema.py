import sqlite3

try:
    conn = sqlite3.connect('./data/renewise.db')
    cur = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='groups'")
    schema = cur.fetchone()[0]
    print("SCHEMA FOR 'groups':")
    print(schema)
    
except Exception as e:
    print("Error:", e)
finally:
    if 'conn' in locals():
        conn.close()
