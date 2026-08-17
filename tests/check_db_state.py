import sqlite3

try:
    conn = sqlite3.connect('./data/renewise.db')
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cur.fetchall()]
    print("EXISTING TABLES:", tables)
    
    for table in ['groups', 'old_groups']:
        if table in tables:
            cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
            count = cur.fetchone()[0]
            print(f"Table '{table}' exists and has {count} rows.")
        else:
            print(f"Table '{table}' DOES NOT exist.")
            
except Exception as e:
    print("Error:", e)
finally:
    if 'conn' in locals():
        conn.close()
