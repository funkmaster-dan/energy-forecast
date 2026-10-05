import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "energy_forecast.api:create_app",
        factory=True,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        workers=1,
        proxy_headers=os.environ.get("ENERGY_TRUST_PROXY", "false").lower() == "true",
        forwarded_allow_ips=os.environ.get("ENERGY_PROXY_IPS", "127.0.0.1"),
    )
