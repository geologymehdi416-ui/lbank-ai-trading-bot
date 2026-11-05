import requests

URL = "https://lbkperp.lbank.com/cfd/openApi/v1/pub/instrument"

try:
    r = requests.get(
        URL,
        params={"productGroup": "SwapU"},
        timeout=20
    )

    print("STATUS:", r.status_code)
    print("RESPONSE:", r.text[:5000])

except Exception as e:
    print("ERROR:", e)