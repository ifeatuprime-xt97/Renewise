import sqlite3

def fix_foreign_keys():
    conn = sqlite3.connect('./data/renewise.db')
    conn.execute("PRAGMA foreign_keys=OFF")
    
    tables_to_fix = ['subscriptions', 'admin_audit_log', 'vault_registry']
    
    for table in tables_to_fix:
        # Get current schema
        cur = conn.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{table}'")
        row = cur.fetchone()
        if not row:
            print(f"Table {table} not found.")
            continue
            
        old_sql = row[0]
        if '"old_groups"' not in old_sql and 'old_groups' not in old_sql:
            print(f"Table {table} does not reference old_groups. Skipping.")
            continue
            
        new_sql = old_sql.replace('"old_groups"', 'groups').replace('old_groups', 'groups')
        
        try:
            print(f"Fixing {table}...")
            conn.execute("BEGIN TRANSACTION")
            conn.execute(f"ALTER TABLE {table} RENAME TO temp_{table}")
            conn.execute(new_sql)
            
            # Get columns to copy
            cur = conn.execute(f"PRAGMA table_info(temp_{table})")
            columns = [col[1] for col in cur.fetchall()]
            cols_str = ', '.join(columns)
            
            conn.execute(f"INSERT INTO {table} ({cols_str}) SELECT {cols_str} FROM temp_{table}")
            conn.execute(f"DROP TABLE temp_{table}")
            conn.commit()
            print(f"Successfully fixed {table}.")
        except Exception as e:
            conn.rollback()
            print(f"Error fixing {table}: {e}")

    conn.execute("PRAGMA foreign_keys=ON")
    conn.close()

if __name__ == '__main__':
    fix_foreign_keys()
