# Employee Attendance & Analytics API

FastAPI + PyMongo attendance service. Requires Python 3.11+ and MongoDB 6.0+.

## 1. Create a MongoDB database (Atlas)

If MongoDB is new to you, Atlas is a straightforward option because the database
runs in the cloud:

1. Go to <https://www.mongodb.com/atlas/database> and create/sign in to an
   account. Create a free cluster.
2. In Atlas, open **Security → Database Access** and add a database user. Save
   its username and password somewhere private. This is a database user, not
   your Atlas website login.
3. Open **Security → Network Access**, add your current IP address, and wait for
   the access-list entry to become active. For safety, do not allow access from
   every IP address.
4. Open the cluster's **Connect** panel, choose **Drivers**, select Python, and
   copy the connection string. It looks similar to
   `mongodb+srv://<username>:<password>@cluster0.example.mongodb.net/`.
5. Replace `<username>` and `<password>` with the database user's credentials.
   For a first setup, choose a password made of letters and numbers so it does
   not need URL-encoding. Keep the completed URI private.

## 2. Install and run (PowerShell)

Open PowerShell in the project folder. Check for Python 3.12 with:

```powershell
py -0p
```

If Python 3.12 is listed, create and activate a virtual environment and install
the project dependencies:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

If Python 3.12 is not listed, install Python 3.12 first, or create the
environment using an already-installed Python 3.12 executable:

```powershell
& 'C:\Users\HP\AppData\Roaming\uv\python\cpython-3.12.12-windows-x86_64-none\python.exe' -m venv .venv
```

If PowerShell says script execution is disabled when activating, run this once
for the current PowerShell window, then activate again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

Set the Atlas URI and database name in this same PowerShell window. Replace the
example URI with the private connection string you copied:

```powershell
$env:MONGO_URI = "mongodb+srv://YOUR_USER:YOUR_PASSWORD@cluster0.example.mongodb.net/?retryWrites=true&w=majority"
$env:MONGO_DB = "attendance_db"
uvicorn app.main:app --port 8000
```

Leave that PowerShell window running. In a browser, open
<http://127.0.0.1:8000/health>. `{"status":"ok"}` means the API reached MongoDB;
then open <http://127.0.0.1:8000/docs> to try the API. If `/health` returns an
error, check the URI, database username/password, and Atlas IP access list.

The `$env:` settings apply only to that PowerShell window; set them again in a
new window. They are not committed to Git. Run unit tests with
`python -m unittest discover -s tests -v`.

The app creates its indexes at startup. Review the business-rule findings in
[REVIEW.md](./REVIEW.md) and design answers in [DECISIONS.md](./DECISIONS.md).
