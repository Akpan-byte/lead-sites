#!/usr/bin/env python3
"""
Shrinks prices.db safely using /dev/shm staging.
Copies pruned data from source prices.db to a clean, fresh /dev/shm/prices_shm.db,
then replaces the source prices.db with the shm database.
"""
import sqlite3
import os
import shutil
import time

SRC_DB = "/config/projects/trading/price-pipeline/price_pipeline/prices.db"
SHM_DB = "/dev/shm/prices_shm.db"

def shrink_database():
    print(f"Starting database shrink process...")
    print(f"Source size: {os.path.getsize(SRC_DB) / 1024 / 1024:.2f} MB")
    
    if os.path.exists(SHM_DB):
        os.remove(SHM_DB)
        
    start_time = time.time()
    
    # 1. Connect to SHM database
    dest_conn = sqlite3.connect(SHM_DB)
    dest_conn.execute("PRAGMA journal_mode = OFF;")
    dest_conn.execute("PRAGMA synchronous = OFF;")
    
    # 2. Attach source database
    dest_conn.execute(f"ATTACH DATABASE '{SRC_DB}' AS src;")
    
    # 3. Get all tables and indexes to create
    cursor = dest_conn.cursor()
    cursor.execute("SELECT type, name, sql FROM src.sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%';")
    items = cursor.fetchall()
    
    # 4. Create all tables first
    for item_type, name, sql in items:
        if item_type == 'table':
            print(f"Creating table {name}...")
            dest_conn.execute(sql)
            
    dest_conn.commit()
    
    # 5. Copy data for each table
    cursor.execute("SELECT name FROM src.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
    tables = [r[0] for r in cursor.fetchall()]
    
    for table in tables:
        print(f"Copying data for {table}...")
        t_start = time.time()
        dest_conn.execute(f"INSERT INTO main.{table} SELECT * FROM src.{table};")
        dest_conn.commit()
        print(f"Finished copying {table} in {time.time() - t_start:.2f}s.")
        
    # 6. Create all indexes
    for item_type, name, sql in items:
        if item_type == 'index':
            print(f"Creating index {name}...")
            t_start = time.time()
            dest_conn.execute(sql)
            dest_conn.commit()
            print(f"Finished index {name} in {time.time() - t_start:.2f}s.")
            
    # 7. Detach and close
    dest_conn.execute("DETACH DATABASE src;")
    dest_conn.close()
    
    shm_size = os.path.getsize(SHM_DB)
    print(f"SHM database size: {shm_size / 1024 / 1024:.2f} MB")
    print(f"Cloning completed in {time.time() - start_time:.2f}s.")
    
    # 8. Safely replace SRC_DB with SHM_DB
    print(f"Replacing source database with compressed version...")
    # Overwrite the original prices.db with prices_shm.db
    shutil.copy2(SHM_DB, SRC_DB)
    
    # 9. Clean up
    os.remove(SHM_DB)
    print(f"Database shrink completed successfully!")
    print(f"New size: {os.path.getsize(SRC_DB) / 1024 / 1024:.2f} MB")

if __name__ == "__main__":
    shrink_database()
