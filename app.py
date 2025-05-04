from flask import Flask, render_template, jsonify, request
import requests
import json
import os
import re

app = Flask(__name__)

TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiIsImtpZCI6IjI4YTMxOGY3LTAwMDAtYTFlYi03ZmExLTJjNzQzM2M2Y2NhNSJ9.eyJpc3MiOiJzdXBlcmNlbGwiLCJhdWQiOiJzdXBlcmNlbGw6Z2FtZWFwaSIsImp0aSI6IjVmMWNlNjljLTg4ZGMtNDU5Mi05YWM2LTM1ZjdlZTNlNWE4OCIsImlhdCI6MTc0NjMyMTY3OSwic3ViIjoiZGV2ZWxvcGVyLzI5ZDU1N2YzLTc1MTUtNDNhYy04YjI4LWYwMzRhMThiN2MxYyIsInNjb3BlcyI6WyJyb3lhbGUiXSwibGltaXRzIjpbeyJ0aWVyIjoiZGV2ZWxvcGVyL3NpbHZlciIsInR5cGUiOiJ0aHJvdHRsaW5nIn0seyJjaWRycyI6WyIyMDEuMC44Mi4yNTMiLCIxMDMuMTk5LjE4NS41NCJdLCJ0eXBlIjoiY2xpZW50In1dfQ._lAJrxXuDCRgqnHwPnzGpI6bq6Pam0hfq9Xd3GSrjbaLqJN6kMdyw56DWzbjYMSlaiaJjuGcize50jC8OAlefQ"
BADGE_MAPPING_URL = "https://royaleapi.github.io/cr-api-data/json/alliance_badges.json"

# Carrega mapeamento de badgeId → name (uma vez, na inicialização)
try:
    badge_mapping = requests.get(BADGE_MAPPING_URL).json()
except Exception as e:
    app.logger.error(f"Erro ao carregar badge mapping: {e}")
    badge_mapping = []

def get_headers():
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json"
    }

def get_cartas_api():
    url = "https://api.clashroyale.com/v1/cards"
    resp = requests.get(url, headers=get_headers())
    return resp.json() if resp.status_code == 200 else {"items": []}

def fetch_batalhas(tag, tipos):
    tag_enc = tag.replace("#", "%23")
    url = f"https://api.clashroyale.com/v1/players/{tag_enc}/battlelog"
    resp = requests.get(url, headers=get_headers())
    if resp.status_code == 200:
        data = resp.json()
        return [b for b in data if b.get("type") in tipos] if tipos else data
    return []

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/tool")
def tool():
    tag = request.args.get("tag", "")
    return render_template("tool.html", player_tag=tag)

@app.route("/api/clan-info")
def clan_info():
    clan_tag = "#G8YQ28JP"
    tag_enc = clan_tag.replace("#", "%23")
    info_url = f"https://api.clashroyale.com/v1/clans/{tag_enc}"

    try:
        info_resp = requests.get(info_url, headers=get_headers())
        info_resp.raise_for_status()
    except Exception as e:
        app.logger.exception("Erro ao buscar informações do clã")
        return jsonify({"error": "Falha ao buscar informações do clã", "detail": str(e)}), 500

    info = info_resp.json()

    # Mapeia badgeId → badge_name
    badge_id = info.get("badgeId")
    badge_name = next((b["name"] for b in badge_mapping if b.get("id") == badge_id), None)
    # Se não encontrar, usa badgeUrls.medium da API oficial
    badge_url = (
        f"https://royaleapi.github.io/cr-api-assets/badges/{badge_name}.png"
        if badge_name
        else info.get("badgeUrls", {}).get("medium")
    )

    # Busca membros do clã
    members_url = f"https://api.clashroyale.com/v1/clans/{tag_enc}/members"
    try:
        members_resp = requests.get(members_url, headers=get_headers())
        members_resp.raise_for_status()
        members_json = members_resp.json()
        members = members_json.get("memberList") or members_json.get("items") or []
    except Exception as e:
        app.logger.exception("Erro ao buscar membros do clã")
        members = []

    return jsonify({
        "clan": {
            "name": info.get("name"),
            "tag": info.get("tag"),
            "membersCount": info.get("members"),
            "clanScore": info.get("clanScore"),
            "clanWarTrophies": info.get("clanWarTrophies"),
            "badgeUrl": badge_url
        },
        "memberList": [
            {
                "name": m.get("name"),
                "tag": m.get("tag"),
                "role": m.get("role"),
                "trophies": m.get("trophies")
            }
            for m in members
        ]
    })

@app.route("/api/tags")
def tags():
    files = [f for f in os.listdir() if f.startswith("historico_") and f.endswith(".json")]
    tags = [re.sub(r"^historico_|\.json$", "", f) for f in files]
    return jsonify(sorted(tags))

@app.route("/api/historico")
def historico():
    tag = request.args.get("tag", "").strip()
    if not tag:
        return jsonify({"erro": "Tag não informada"}), 400

    tipos = request.args.getlist("type")
    safe = re.sub(r"\W+", "", tag)
    arquivo = f"historico_{safe}.json"

    historico = []
    if os.path.exists(arquivo):
        with open(arquivo, "r", encoding="utf-8") as f:
            historico = json.load(f)

    batalhas = fetch_batalhas(tag, tipos)
    seen = {h["battleTime"] + h["team"][0]["tag"] for h in historico}
    novas = [b for b in batalhas if (b["battleTime"] + b["team"][0]["tag"]) not in seen]

    if novas:
        historico += novas
        with open(arquivo, "w", encoding="utf-8") as f:
            json.dump(historico, f, ensure_ascii=False, indent=4)

    with open(arquivo, "r", encoding="utf-8") as f:
        historico_atualizado = json.load(f)

    return jsonify(historico_atualizado)

@app.route("/api/cartas")
def cartas():
    return jsonify(get_cartas_api())

@app.route("/api/estatisticas-cartas")
def estatisticas_cartas():
    tag = request.args.get("tag", "").strip()
    if not tag:
        return jsonify({"erro": "Tag não informada"}), 400

    safe = re.sub(r"\W+", "", tag)
    arquivo = f"historico_{safe}.json"
    if not os.path.exists(arquivo):
        return jsonify({"erro": "Sem histórico ainda."}), 404

    with open(arquivo, "r", encoding="utf-8") as f:
        historico = json.load(f)

    cartas_stats = {}
    for batalha in historico:
        team = batalha['team'][0]
        opponent = batalha['opponent'][0]
        for carta in team['cards']:
            nome = carta['name']
            stats = cartas_stats.setdefault(nome, {"usos": 0, "vitorias": 0, "derrotas": 0})
            stats["usos"] += 1
            if team['crowns'] > opponent['crowns']:
                stats["vitorias"] += 1
            else:
                stats["derrotas"] += 1
        for carta in opponent['cards']:
            nome = carta['name']
            stats = cartas_stats.setdefault(nome, {"usos": 0, "vitorias": 0, "derrotas": 0})
            stats["usos"] += 1
            if opponent['crowns'] > team['crowns']:
                stats["vitorias"] += 1
            else:
                stats["derrotas"] += 1

    resultado = []
    for nome, stats in cartas_stats.items():
        total = stats["usos"]
        winrate = (stats["vitorias"] / total) * 100 if total > 0 else 0
        resultado.append({
            "carta": nome,
            "usos": total,
            "vitorias": stats["vitorias"],
            "derrotas": stats["derrotas"],
            "winrate": round(winrate, 1)
        })
    resultado.sort(key=lambda x: (-x["winrate"], -x["usos"]))
    return jsonify(resultado)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)

