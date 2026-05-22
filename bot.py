import os, time, logging, requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("bot")

POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "60"))

def run():
    log.info("Bot demarre!")
    while True:
        try:
            log.info("Scan en cours...")
            r = requests.get("https://gamma-api.polymarket.com/markets", 
                           params={"active": "true", "limit": 5}, timeout=10)
            if r.ok:
                markets = r.json()
                log.info("Marches trouves: " + str(len(markets)))
            else:
                log.error("Erreur API: " + str(r.status_code))
        except Exception as e:
            log.error("Erreur: " + str(e))
        log.info("Attente " + str(int(POLL_INTERVAL)) + "s...")
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    run()
