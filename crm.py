"""Lancé par GitHub Actions quand le CRM le demande : une tâche = un lancement.

Le CRM (Cloudflare) dit quoi faire : connecter un compte, poster l'annonce, lister les groupes...
Ce script le fait avec le compte Telegram, puis renvoie le résultat au CRM.

Secrets GitHub nécessaires : API_ID, API_HASH, CRM_URL, RUNNER_TOKEN, SESSION_KEY
(+ SESSION, l'ancienne clé du compte principal, seulement pour l'importer une fois).
"""
import asyncio
import base64
import io
import json
import logging
import os
import random
import sys
import time
import urllib.error
import urllib.request

from cryptography.fernet import Fernet, InvalidToken
from telethon import TelegramClient, errors, functions, types, utils
from telethon.sessions import StringSession

logging.basicConfig(format="%(asctime)s - %(message)s", level=logging.INFO)
logging.getLogger("telethon").setLevel(logging.WARNING)
log = logging.info

CRM_URL = os.environ["CRM_URL"].rstrip("/")
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
FERNET = Fernet(os.environ["SESSION_KEY"].encode())
PAUSE_MIN = int(os.environ.get("PAUSE_MIN") or 30)
PAUSE_MAX = int(os.environ.get("PAUSE_MAX") or 90)

# Erreurs Telegram -> (statut, texte affiché dans le CRM)
ERREURS = {
    "ChatWriteForbiddenError": ("write_forbidden", "Écriture réservée aux admins"),
    "ChatAdminRequiredError": ("write_forbidden", "Écriture réservée aux admins"),
    "ChatRestrictedError": ("write_forbidden", "Écriture limitée par les admins"),
    "ChatSendPlainForbiddenError": ("write_forbidden", "Messages texte interdits dans ce groupe"),
    "ChatGuestSendForbiddenError": ("write_forbidden", "Il faut d'abord rejoindre le groupe"),
    # Telegram a limité le compte (signalé pour spam) : il ne peut plus écrire dans les groupes
    "UserBannedInChannelError": ("limited", "Compte limité par Telegram (spam) : vérifie avec @SpamBot"),
    "ChannelPrivateError": ("banned", "Exclu par les admins (ou groupe supprimé)"),
    "UserNotParticipantError": ("banned", "Plus membre du groupe"),
    "ChatIdInvalidError": ("banned", "Groupe introuvable"),
    "PeerIdInvalidError": ("banned", "Groupe introuvable"),
    "SlowModeWaitError": ("flood", "Mode lent actif"),
    "FloodWaitError": ("flood", "Limite Telegram"),
}
SESSION_MORTE = ("AuthKeyUnregisteredError", "SessionRevokedError", "UserDeactivatedError",
                 "UserDeactivatedBanError", "AuthKeyDuplicatedError", "SessionExpiredError")


# ------------------------------------------------------------ échanges avec le CRM
def crm(method, path, data=None):
    req = urllib.request.Request(
        CRM_URL + path, method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"authorization": "Bearer " + os.environ["RUNNER_TOKEN"], "content-type": "application/json",
                 "user-agent": "crm-runner"},
    )
    for essai in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            if e.code < 500:
                raise SystemExit(f"Le CRM a refusé {path} ({e.code}) : {detail}")
        except urllib.error.URLError:
            pass
        time.sleep(3 * (essai + 1))
    raise SystemExit(f"CRM injoignable : {path}")


def chiffrer(session):
    return FERNET.encrypt(session.encode()).decode()


def dechiffrer(session_enc):
    try:
        return FERNET.decrypt(session_enc.encode()).decode()
    except (InvalidToken, AttributeError):
        return ""


def client_pour(session_str):
    return TelegramClient(StringSession(session_str), API_ID, API_HASH,
                          device_model="CRM Recrutement", system_version="GitHub", app_version="1.0")


# ------------------------------------------------------------ connexion d'un compte
async def login_send(job, acc):
    client = client_pour("")
    await client.connect()
    try:
        sent = await client.send_code_request(acc["phone"])
    except errors.PhoneNumberInvalidError:
        return etat(job, "error", "Numéro invalide (format +33…)")
    except errors.FloodWaitError as e:
        return etat(job, "error", f"Trop d'essais, réessaie dans {e.seconds // 60 + 1} min")
    crm("POST", f"/runner/job/{job['id']}/account", {
        "session_enc": chiffrer(client.session.save()), "login_hash": sent.phone_code_hash,
        "login_state": "code_sent", "login_error": None,
    })
    await client.disconnect()
    log("Code envoyé au %s", acc["phone"][:4] + "…")


async def login_code(job, acc):
    client = client_pour(dechiffrer(acc["session_enc"]))
    await client.connect()
    try:
        await client.sign_in(acc["phone"], acc["login_code"], phone_code_hash=acc["login_hash"])
    except errors.SessionPasswordNeededError:
        crm("POST", f"/runner/job/{job['id']}/account", {
            "session_enc": chiffrer(client.session.save()), "login_state": "need_password", "login_error": None})
        return
    except (errors.PhoneCodeInvalidError, errors.PhoneCodeEmptyError):
        return etat(job, "code_sent", "Code incorrect, réessaie")
    except errors.PhoneCodeExpiredError:
        return etat(job, "error", "Code expiré : clique sur Reconnecter")
    await connecte(job, client)


async def login_password(job, acc):
    client = client_pour(dechiffrer(acc["session_enc"]))
    await client.connect()
    try:
        await client.sign_in(password=acc["login_password"])
    except errors.PasswordHashInvalidError:
        return etat(job, "need_password", "Mot de passe incorrect")
    await connecte(job, client)


async def import_session(job, acc):
    """Reprend le compte déjà branché sur GitHub (secret SESSION) sans refaire la connexion."""
    session = os.environ.get("SESSION", "")
    if not session:
        return etat(job, "error", "Le secret SESSION n'existe pas sur GitHub")
    client = client_pour(session)
    await client.connect()
    if not await client.is_user_authorized():
        return etat(job, "error", "Le secret SESSION n'est plus valide : ajoute le compte avec son numéro")
    await connecte(job, client, sync_folder=job["payload"].get("folder", ""))


async def connecte(job, client, sync_folder=None):
    me = await client.get_me()
    crm("POST", f"/runner/job/{job['id']}/account", {
        "session_enc": chiffrer(client.session.save()), "status": "connected", "login_state": "connected",
        "login_error": None, "login_hash": None, "tg_user_id": me.id, "tg_username": me.username,
        "phone": "+" + me.phone if me.phone else None,
    })
    log("Compte connecté : %s", me.first_name)
    await envoyer_groupes(job, client, activate_folder=sync_folder)  # on liste ses groupes tout de suite


def etat(job, login_state, message):
    crm("POST", f"/runner/job/{job['id']}/account", {"login_state": login_state, "login_error": message})
    raise Echec(message)


class Echec(Exception):
    pass


# ------------------------------------------------------------ groupes
async def dossiers(client):
    """chat_id -> noms des dossiers Telegram qui le contiennent."""
    res = await client(functions.messages.GetDialogFiltersRequest())
    out = {}
    for f in getattr(res, "filters", res):
        if not isinstance(f, (types.DialogFilter, types.DialogFilterChatlist)):
            continue
        titre = f.title.text if hasattr(f.title, "text") else f.title
        for peer in list(f.pinned_peers) + list(f.include_peers):
            out.setdefault(utils.get_peer_id(peer), []).append(titre)
    return out


async def envoyer_groupes(job, client, activate_folder=None):
    par_dossier = await dossiers(client)
    groupes = []
    async for d in client.iter_dialogs():
        e = d.entity
        if isinstance(e, types.Chat) and not e.deactivated or isinstance(e, types.Channel) and e.megagroup:
            groupes.append({
                "chat_id": d.id, "title": d.name, "members": getattr(e, "participants_count", None),
                "link": f"https://t.me/{e.username}" if getattr(e, "username", None) else None,
                "folders": "|".join(par_dossier.get(d.id, [])),
            })
    crm("POST", f"/runner/job/{job['id']}/groups", {"groups": groupes, "activate_folder": activate_folder or ""})
    log("%d groupe(s) envoyés au CRM", len(groupes))
    return len(groupes)


def lire_lien(lien):
    s = lien.strip()
    for p in ("https://", "http://", "www."):
        s = s.removeprefix(p)
    for p in ("t.me/", "telegram.me/"):
        s = s.removeprefix(p)
    s = s.split("?")[0].strip("/")
    if s.startswith("@"):
        return "username", s[1:], 0
    if s.startswith("+"):
        return "invite", s[1:], 0
    if s.startswith("joinchat/"):
        return "invite", s.split("/")[1], 0
    parts = s.split("/")
    if parts[0] == "c" and len(parts) > 1 and parts[1].isdigit():
        return "id", int("-100" + parts[1]), int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    return "username", parts[0], int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0


async def rejoindre(job, client):
    lien = job["payload"]["link"]
    genre, valeur, sujet = lire_lien(lien)
    try:
        if genre == "invite":
            info = await client(functions.messages.CheckChatInviteRequest(valeur))
            if isinstance(info, (types.ChatInviteAlready, types.ChatInvitePeek)):
                entite = info.chat
            else:
                entite = (await client(functions.messages.ImportChatInviteRequest(valeur))).chats[0]
        else:
            entite = await client.get_entity(valeur)
            if isinstance(entite, types.Channel) and entite.left:
                await client(functions.channels.JoinChannelRequest(entite))
    except errors.InviteRequestSentError:
        raise Echec("Demande d'adhésion envoyée : l'admin doit l'accepter, puis réessaie")
    except (errors.InviteHashExpiredError, errors.InviteHashInvalidError):
        raise Echec("Lien d'invitation expiré ou invalide")
    except (ValueError, errors.UsernameNotOccupiedError, errors.UsernameInvalidError):
        raise Echec("Groupe introuvable avec ce lien")
    except errors.ChannelsTooMuchError:
        raise Echec("Ce compte est déjà dans trop de groupes")
    if sujet and not getattr(entite, "forum", False):
        sujet = 0
    crm("POST", f"/runner/job/{job['id']}/groups", {
        "groups": [{"chat_id": utils.get_peer_id(entite), "title": entite.title, "topic_id": sujet, "link": lien,
                    "members": getattr(entite, "participants_count", None)}],
        "activate_all": True, "type_id": job["payload"].get("type_id"),
    })
    return {"title": entite.title}


# ------------------------------------------------------------ profil (photo, prénom, nom, bio)
async def profil(job, client):
    p = job["payload"]
    if any(k in p for k in ("first_name", "last_name", "about")):
        await client(functions.account.UpdateProfileRequest(
            first_name=p.get("first_name") or None, last_name=p.get("last_name"), about=p.get("about")))
    if p.get("photo"):
        image = io.BytesIO(base64.b64decode(p["photo"].split(",", 1)[1]))
        image.name = "photo.jpg"
        fichier = await client.upload_file(image)
        await client(functions.photos.UploadProfilePhotoRequest(file=fichier))
    me = await client.get_me()
    nom = getattr(me, "last_name", None) or ""
    log("Profil mis à jour : %s %s", me.first_name, nom)
    return {"first_name": me.first_name, "last_name": nom}


# ------------------------------------------------------------ annonces
async def poster(job, client, groupes):
    attente = job["not_before"] - time.time()
    if attente > 0:
        log("En avance, on attend %d s pour poster pile à l'heure.", attente)
        await asyncio.sleep(attente)
    ok = 0
    for i, g in enumerate(groupes):
        if i:
            await asyncio.sleep(random.randint(PAUSE_MIN, PAUSE_MAX))
        statut, detail = await envoyer(client, g)
        crm("POST", f"/runner/job/{job['id']}/post", {"group_id": g["id"], "status": statut, "detail": detail,
                                                      "topic_id": g["topic_id"] if g.get("nouveau_sujet") else None})
        log("%s %s%s", "✅" if statut == "sent" else "❌", g["title"], f" : {detail}" if detail else "")
        ok += statut == "sent"
        if statut == "disconnected":
            crm("POST", f"/runner/job/{job['id']}/account", {"status": "disconnected", "login_state": "idle"})
            raise Echec("Le compte a été déconnecté de Telegram")
        if statut == "limited":
            # inutile d'insister dans les autres groupes : on arrête pour ne pas aggraver la limitation
            raise Echec("Compte limité par Telegram (spam) : envois arrêtés. Vérifie avec @SpamBot")
    return {"sent": ok, "total": len(groupes)}


MOTS_SUJET = ("job", "recrut", "annonce", "offre", "emploi", "travail", "mission", "pub", "promo", "business", "staff", "va ", "vas ", "agence")


async def sujet_ouvert(client, entite):
    """Groupe à sujets (forum) : renvoie (id, titre) d'un sujet ouvert, de préférence un sujet « jobs / annonces »."""
    res = await client(functions.messages.GetForumTopicsRequest(peer=entite, offset_date=None, offset_id=0, offset_topic=0, limit=100))
    ouverts = [t for t in res.topics if isinstance(t, types.ForumTopic) and not t.closed and not t.hidden]
    for t in ouverts:
        if any(m in (t.title.lower() + " ") for m in MOTS_SUJET):
            return t.id, t.title
    return (ouverts[0].id, ouverts[0].title) if ouverts else (None, None)


async def envoyer(client, g):
    for essai in (1, 2):
        try:
            try:
                entite = await client.get_entity(g["chat_id"])
            except ValueError:
                entite = await client.get_entity(lire_lien(g["link"])[1]) if g.get("link") else None
                if entite is None:
                    raise
            await client.send_message(entite, g["text"], reply_to=g.get("topic_id") or None, link_preview=False)
            return "sent", (f"Posté dans le sujet « {g['nouveau_sujet']} »" if g.get("nouveau_sujet") else None)
        except errors.RPCError as e:
            nom = type(e).__name__
            if nom in SESSION_MORTE:
                return "disconnected", "Compte déconnecté"
            if "TOPIC_CLOSED" in str(e) and essai == 1:
                # le sujet « Général » est fermé : on poste dans un sujet ouvert, et le CRM le retient pour la suite
                try:
                    sid, titre = await sujet_ouvert(client, entite)
                except errors.RPCError:
                    sid, titre = None, None
                if not sid:
                    return "write_forbidden", "Sujet fermé et aucun sujet ouvert"
                g["topic_id"], g["nouveau_sujet"] = sid, titre
                log("Sujet fermé : on essaie le sujet « %s »", titre)
                continue
            if "TOPIC_CLOSED" in str(e):
                return "write_forbidden", "Sujet fermé"
            secondes = getattr(e, "seconds", 0) or 0
            if nom == "FloodWaitError" and essai == 1 and secondes <= 300:
                log("Telegram demande d'attendre %ss, on attend.", secondes)
                await asyncio.sleep(secondes + 2)
                continue
            statut, texte = ERREURS.get(nom, ("error", "Erreur"))
            return statut, texte + (f" ({secondes} s)" if secondes else "") + ("" if nom in ERREURS else f" : {nom}")
        except ValueError:
            return "banned", "Groupe introuvable"
    return "error", "Échec"


# ------------------------------------------------------------ programme principal
async def main(job_id):
    data = crm("GET", f"/runner/job/{job_id}")
    job, acc = data["job"], data["account"]
    log("Tâche %s : %s (compte %s)", job_id, job["type"], acc["name"] if acc else "?")
    resultat, client = None, None
    try:
        if job["type"] == "login_send":
            await login_send(job, acc)
        elif job["type"] == "login_code":
            await login_code(job, acc)
        elif job["type"] == "login_password":
            await login_password(job, acc)
        elif job["type"] == "import":
            await import_session(job, acc)
        else:
            client = client_pour(dechiffrer(acc["session_enc"]))
            await client.connect()
            if not await client.is_user_authorized():
                crm("POST", f"/runner/job/{job_id}/account", {"status": "disconnected", "login_state": "idle"})
                raise Echec("Le compte n'est plus connecté : clique sur Reconnecter dans le CRM")
            if job["type"] == "sync":
                resultat = {"groups": await envoyer_groupes(job, client)}
            elif job["type"] == "join":
                resultat = await rejoindre(job, client)
            elif job["type"] == "profile":
                resultat = await profil(job, client)
            elif job["type"] == "test":
                await client.send_message("me", data["text"], link_preview=False)
            elif job["type"] == "post":
                resultat = await poster(job, client, data["groups"])
        crm("POST", f"/runner/job/{job_id}/done", {"ok": True, "result": resultat})
    except Echec as e:
        crm("POST", f"/runner/job/{job_id}/done", {"ok": False, "error": str(e)})
        log("❌ %s", e)
    except Exception as e:  # noqa: BLE001
        crm("POST", f"/runner/job/{job_id}/done", {"ok": False, "error": f"{type(e).__name__} : {e}"[:300]})
        raise
    finally:
        if client:
            await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
