# Truncated to free up disk space. Purges temporary files.
import shutil
import os
temp_dir = "/config/bardfx-strategy/data/temp"
if os.path.exists(temp_dir):
    shutil.rmtree(temp_dir)
print("Temporary directory successfully purged!")
