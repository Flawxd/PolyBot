import os

from dotenv import load_dotenv
from py_clob_client.client import ClobClient

load_dotenv()

PRIVATE_KEY = os.getenv("PRIVATE_KEY")


def generate_creds():
    host = "https://clob.polymarket.com"
    chain_id = 137

    client = ClobClient(host, key=PRIVATE_KEY, chain_id=chain_id)

    try:
        creds = client.create_or_derive_api_creds()

        print("\nSUCCESS! Here are your API Credentials:")
        print("------------------------------------------------")
        print(f"POLY_API_KEY={creds.api_key}")
        print(f"POLY_API_SECRET={creds.api_secret}")
        print(f"POLY_API_PASSPHRASE={creds.api_passphrase}")
        print("------------------------------------------------")
        print("Copy these 3 lines into your .env file")

    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    generate_creds()
