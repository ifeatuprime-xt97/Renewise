import sqlite3
db = sqlite3.connect('./data/renewise.db')
rows = db.execute('SELECT tx_hash, sub_id, processed_at FROM processed_tx_hashes').fetchall()
print('=== processed_tx_hashes ===')
for r in rows:
    print(r)
print(f'Total rows: {len(rows)}')
target = 'u5Qqanb3upvCudR+V5DDcUUTlKy+E6J+RPNmElyWUF0='
found = db.execute('SELECT 1 FROM processed_tx_hashes WHERE tx_hash=?', (target,)).fetchone()
print(f'\nTarget tx present: {found is not None}')
db.close()
