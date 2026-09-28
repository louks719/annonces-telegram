"""Lancé par GitHub Actions : poste l'annonce dans les groupes du dossier Telegram.

Les codes secrets viennent des "Secrets" GitHub (API_ID, API_HASH, SESSION).
    python poster.py verifier   -> se connecte et liste les groupes, sans rien poster
    python poster.py poster     -> poste l'annonce
"""
import asyncio
import datetime
import logging
import os
import random
import sys
from zoneinfo import ZoneInfo

from telethon import TelegramClient, functions, types
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession

import reglages

logging.basicConfig(format="%(asctime)s - %(message)s", level=logging.INFO)
logging.getLogger("telethon").setLevel(logging.WARNING)

PARIS = ZoneInfo("Europe/Paris")
JOURS_NUM = {"lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3, "vendredi": 4, "samedi": 5, "dimanche": 6}


def titre_dossier(f) -> str:
    return f.title.text if hasattr(f.title, "text") else f.title


async def groupes_du_dossier(client) -> dict:
    res = await client(functions.messages.GetDialogFiltersRequest())
    for f in getattr(res, "filters", res):
        if not isinstance(f, (types.DialogFilter, types.DialogFilterChatlist)):
            continue
        if titre_dossier(f).strip().lower() != reglages.DOSSIER.strip().lower():
            continue
        groupes = {}
        for peer in list(f.pinned_peers) + list(f.include_peers):
            entite = await client.get_entity(peer)
            if isinstance(entite, types.Chat) or (isinstance(entite, types.Channel) and entite.megagroup):
                groupes[entite.title] = entite
        return groupes
    raise SystemExit(f"Dossier introuvable : {reglages.DOSSIER}")


async def poster_partout(client, groupes: dict) -> None:
    for i, (nom, groupe) in enumerate(groupes.items()):
        if i > 0:
            await asyncio.sleep(random.randint(reglages.PAUSE_MIN, reglages.PAUSE_MAX))
        try:
            await client.send_message(groupe, reglages.ANNONCE)
            logging.info("✅ Posté dans : %s", nom)
        except FloodWaitError as e:
            logging.warning("Telegram demande d'attendre %ss, on attend.", e.seconds)
            await asyncio.sleep(e.seconds)
        except RPCError as e:
            logging.warning("❌ Impossible de poster dans %s : %s", nom, e)


async def attendre_midi() -> bool:
    """Pour les lancements automatiques : GitHub lance 2 horaires (heure d'été / d'hiver).
    Renvoie False si ce lancement n'est pas le bon, sinon attend midi pile si on est en avance."""
    cron = os.environ.get("CRON", "")
    if not cron:
        return True  # lancement manuel : on poste tout de suite

    maintenant = datetime.datetime.now(PARIS)
    if maintenant.weekday() not in {JOURS_NUM[j.strip().lower()] for j in reglages.JOURS}:
        logging.info("Pas d'annonce aujourd'hui (jour non choisi).")
        return False

    heure_utc_cron = int(cron.split()[1])
    decalage_paris = int(maintenant.utcoffset().total_seconds() // 3600)  # 2 en été, 1 en hiver
    if heure_utc_cron + decalage_paris != 11:  # le cron part à 11h50, heure de Paris
        logging.info("Lancement pour l'autre saison (été/hiver), rien à faire.")
        return False

    midi = maintenant.replace(hour=12, minute=0, second=0, microsecond=0)
    if maintenant < midi:
        logging.info("En avance, on attend midi.")
        await asyncio.sleep((midi - maintenant).total_seconds())
    return True


async def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "poster"
    client = TelegramClient(StringSession(os.environ["SESSION"]), int(os.environ["API_ID"]), os.environ["API_HASH"])
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit("La SESSION n'est plus valide : il faut en regénérer une.")

    moi = await client.get_me()
    logging.info("Connecté en tant que %s.", moi.first_name)
    groupes = await groupes_du_dossier(client)
    logging.info("Dossier « %s » : %d groupe(s) : %s", reglages.DOSSIER, len(groupes), ", ".join(groupes))

    if mode == "poster" and await attendre_midi():
        await poster_partout(client, groupes)

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
