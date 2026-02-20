import zipfile, os, sys

pwd = os.getenv("ZIP_F")
if not pwd:
    sys.exit("Missing ZIP")

try:
    with zipfile.ZipFile("files.zip") as z:
        z.extractall("workspace", pwd=pwd.encode())
except RuntimeError:
    sys.exit("Wrong ZIPP")
except Exception as e:
    sys.exit(f"Extraction failed:")
