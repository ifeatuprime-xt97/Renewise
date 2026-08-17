import sqlite3

try:
    conn = sqlite3.connect('./data/renewise.db')
    cur = conn.execute("SELECT type, name, sql FROM sqlite_master WHERE sql LIKE '%old_groups%'")
    rows = cur.fetchall()
    print("OBJECTS REFERENCING 'old_groups':")
    for row in rows:
        print(row)
        
except Exception as e:
    print("Error:", e)
finally:
    if 'conn' in locals():
        conn.close()
