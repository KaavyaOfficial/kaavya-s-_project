import sqlite3
import os

DATABASE = "momentum_fc.db"

def check_db():
    if not os.path.exists(DATABASE):
        print(f"Error: {DATABASE} does not exist.")
        return
    
    try:
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()
        print(f"Tables: {tables}")
        
        for table in tables:
            name = table[0]
            cursor.execute(f"SELECT COUNT(*) FROM {name}")
            count = cursor.fetchone()[0]
            print(f"Table '{name}' has {count} rows.")
            
            if name == 'snapshots':
                cursor.execute("SELECT * FROM snapshots ORDER BY timestamp DESC LIMIT 5")
                rows = cursor.fetchall()
                print("Latest snapshots (id, match_id, timestamp, minute, score_h, score_a, p_index):")
                for r in rows:
                    print(f"  {r}")
        
        conn.close()
    except Exception as e:
        print(f"Error checking DB: {e}")

if __name__ == "__main__":
    check_db()
