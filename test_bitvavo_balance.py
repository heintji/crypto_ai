import time, hmac, hashlib, os, requests

API_KEY = os.getenv("BITVAVO_API_KEY")
API_SECRET = os.getenv("BITVAVO_API_SECRET")

timestamp = str(int(time.time() * 1000))
method = "GET"
path = "/v2/balance"
body = ""

message = timestamp + method + path + body
signature = hmac.new(
    API_SECRET.encode(),
    message.encode(),
    hashlib.sha256
).hexdigest()

headers = {
    "Bitvavo-Access-Key": API_KEY,
    "Bitvavo-Access-Signature": signature,
    "Bitvavo-Access-Timestamp": timestamp,
    "Bitvavo-Access-Window": "10000",
    "Content-Type": "application/json",
}

r = requests.get("https://api.bitvavo.com/v2/balance", headers=headers)
print(r.status_code, r.text)
