import sys
from pathlib import Path
project_root = str(Path(__file__).parent.parent)
sys.path.insert(0, project_root)
import shelve
import os
import requests
from config import settings

def destroy_tokens():
    try:
        # Open the session shelf
        with shelve.open('session_shelf') as shelf:
            # Get stored tokens
            sandbox_session = shelf.get('sandbox_session_token')
            prod_session = shelf.get('production_session_token')
            
            # Destroy session tokens via API
            if sandbox_session:
                print(f"Destroying sandbox session token...")
                requests.delete(
                    f"{settings.TASTY_SANDBOX_BASE_URL}/sessions",
                    headers={"Authorization": sandbox_session}
                )
                
            if prod_session:
                print(f"Destroying production session token...")
                requests.delete(
                    f"{settings.TASTY_PRODUCTION_BASE_URL}/sessions",
                    headers={"Authorization": prod_session}
                )
            
            # Clear all tokens from shelf
            shelf.clear()
            print("Cleared all tokens from session shelf")
        
    except Exception as e:
        print(f"Error destroying tokens: {e}")

if __name__ == "__main__":
    destroy_tokens()