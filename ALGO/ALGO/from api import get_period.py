import requests
import json

BASE_URL = "http://localhost:9999/v1"

API_KEY = {
    "X-API-Key": "Z81JOMHP"
}

response = requests.get(
    f"{BASE_URL}/case",
    headers=API_KEY
)

print("Status code:", response.status_code)

if response.status_code == 200:
    data = response.json()

    print(
        json.dumps(
            data,
            indent=4
        )
    )

else:
    print(response.text)