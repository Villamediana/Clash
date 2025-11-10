from flask import Flask, render_template, jsonify, request
import requests
import json
import os
import re
import time
from datetime import datetime, timezone
import threading

app = Flask(__name__)

# Config base
CLAN_TAG = "#G9QY8GPR"


TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiIsImtpZCI6IjI4YTMxOGY3LTAwMDAtYTFlYi03ZmExLTJjNzQzM2M2Y2NhNSJ9.eyJpc3MiOiJzdXBlcmNlbGwiLCJhdWQiOiJzdXBlcmNlbGw6Z2FtZWFwaSIsImp0aSI6IjRiNDYzOTRmLWJhN2UtNDhiYS1hNTY0LWQ0NDdjZjRlYTk0MSIsImlhdCI6MTc2MDIwMTYxNywic3ViIjoiZGV2ZWxvcGVyLzI5ZDU1N2YzLTc1MTUtNDNhYy04YjI4LWYwMzRhMThiN2MxYyIsInNjb3BlcyI6WyJyb3lhbGUiXSwibGltaXRzIjpbeyJ0aWVyIjoiZGV2ZWxvcGVyL3NpbHZlciIsInR5cGUiOiJ0aHJvdHRsaW5nIn0seyJjaWRycyI6WyIxMDMuMTk5LjE4NS41NCJdLCJ0eXBlIjoiY2xpZW50In1dfQ.hrj4c012WJ3YunuBL2AzroxSG_SxQv8QG0VDpN6IH1YyuNymNW6m92GEjyfKH-yZBuH8Pdzz8HmuWVnGoKJcYA"
BADGE_MAPPING_URL = "https://royaleapi.github.io/cr-api-data/json/alliance_badges.json"

# Carrega mapeamento de badgeId → name (uma vez, na inicialização)
try:
    badge_mapping = requests.get(BADGE_MAPPING_URL).json()
except Exception as e:
    app.logger.error(f"Erro ao carregar badge mapping: {e}")
    badge_mapping = []

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_fame_history(path: str = "fame_history.json") -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"meta": {"lastRaceKey": None, "lastRanks": {}}, "players": {}, "snapshots": {}}


def _save_fame_history(data: dict, path: str = "fame_history.json") -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _deck_key(deck: list) -> str:
    """Gera uma chave estável para um deck, priorizando IDs (mais robusto que nomes)."""
    try:
        parts = []
        for c in (deck or []):
            cid = c.get("id")
            parts.append(str(cid) if cid is not None else (c.get("name") or ""))
        parts = [p for p in parts if p]
        parts.sort()
        return ",".join(parts)
    except Exception:
        return ""


def _cards_key(cards: list) -> str:
    """Chave do deck a partir de uma lista de cartas de uma partida (team[*].cards)."""
    try:
        parts = []
        for c in (cards or []):
            cid = c.get("id")
            parts.append(str(cid) if cid is not None else (c.get("name") or ""))
        parts = [p for p in parts if p]
        parts.sort()
        return ",".join(parts)
    except Exception:
        return ""


def _derive_race_key(rr_json: dict) -> str:
    """Gera uma chave ESTÁVEL para a GUERRA (não por dia).

    A API de River Race expõe campos como:
      - periodType: tipo do período (ex.: "warDay")
      - sectionIndex: índice da seção/corrida dentro da temporada
      - periodIndex: índice do DIA dentro da corrida (muda diariamente)
      - seasonId: id da temporada

    Para acumular medalhas corretamente ao longo de toda a guerra, a chave
    não deve incluir o periodIndex (dia). Assim, o delta é calculado como
    curr - prev ao longo de vários dias até a guerra encerrar.
    """
    period_type = rr_json.get("periodType") or rr_json.get("sectionType") or "unknown"
    section_index = rr_json.get("sectionIndex")
    season_id = rr_json.get("seasonId") or rr_json.get("season")

    # chave estável por GUERRA (sem periodIndex)
    base = f"{period_type}-{section_index}-{season_id}"
    if base.replace("None", "").strip("-"):
        return base
    # fallback: aproxima semana do ano para evitar quebrar acumulação
    return f"wk-{time.gmtime().tm_year}-{time.gmtime().tm_yday // 7}"


def _update_fame_history(rr_json: dict, participants: list) -> tuple[dict, dict, str]:
    """Atualiza histórico; retorna (totais_por_tag, rank_delta_por_tag, race_key)."""
    history = _load_fame_history()
    race_key = _derive_race_key(rr_json)
    snapshots = history.setdefault("snapshots", {})
    prev_snapshot = snapshots.get(race_key, {})
    # Salva metadados do período para rótulos
    period_meta = history.setdefault("periodMeta", {})
    period_meta[race_key] = {
        "periodType": rr_json.get("periodType"),
        "sectionIndex": rr_json.get("sectionIndex"),
        "periodIndex": rr_json.get("periodIndex"),
        "seasonId": rr_json.get("seasonId")
    }

    # acumula deltas
    for p in participants:
        tag = p.get("tag")
        if not tag:
            continue
        curr = int(p.get("fame") or 0)
        prev = int(prev_snapshot.get(tag) or 0)
        delta = max(0, curr - prev)

        player = history.setdefault("players", {}).setdefault(tag, {"name": p.get("name"), "total": 0, "by_period": {}, "timeline": []})
        # mantém nome atualizado
        if p.get("name"):
            player["name"] = p["name"]
        # atualiza totais
        player["total"] = int(player.get("total", 0)) + int(delta)
        byp = player["by_period"].setdefault(race_key, {"total": 0})
        byp["total"] = int(byp.get("total", 0)) + int(delta)
        if delta > 0:
            player["timeline"].append({"ts": _now_iso(), "raceKey": race_key, "delta": delta, "current": curr})

        # atualiza snapshot atual
        prev_snapshot[tag] = curr

    # salva snapshot novo
    snapshots[race_key] = prev_snapshot

    # calcula ranking e deltas
    totals = {t: history["players"][t]["total"] for t in history.get("players", {})}
    ordered = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    new_ranks = {tag: i + 1 for i, (tag, _val) in enumerate(ordered)}
    last_ranks = history.get("meta", {}).get("lastRanks", {})
    rank_delta = {tag: (last_ranks.get(tag, new_ranks[tag]) - new_ranks[tag]) for tag in new_ranks}

    # persiste metadados de mudança de ranking (direção e timestamp) por jogador
    for tag, delta in rank_delta.items():
        try:
            player = history.setdefault("players", {}).setdefault(tag, {"name": None, "total": 0, "by_period": {}, "timeline": []})
            if delta != 0:
                player["rankLastDelta"] = 1 if delta > 0 else -1
                player["rankLastChangeTs"] = _now_iso()
        except Exception:
            pass
    history.setdefault("meta", {})["lastRanks"] = new_ranks
    history["meta"]["lastRaceKey"] = race_key

    _save_fame_history(history)

    return totals, rank_delta, race_key


def _update_members_first_seen(members: list) -> None:
    """Atualiza firstSeen/lastSeen para os membros do clã no histórico."""
    history = _load_fame_history()
    players = history.setdefault("players", {})
    now_iso = _now_iso()
    for m in members or []:
        tag = m.get("tag")
        if not tag:
            continue
        entry = players.setdefault(tag, {"name": m.get("name"), "total": 0, "by_period": {}, "timeline": []})
        if m.get("name"):
            entry["name"] = m["name"]
        if not entry.get("firstSeen"):
            entry["firstSeen"] = now_iso
        entry["lastSeen"] = now_iso
        # histórico de cargo / promoções
        try:
            current_role = m.get("role")
            if current_role:
                if entry.get("lastRole") != current_role:
                    entry.setdefault("roleTimeline", []).append({"ts": now_iso, "role": current_role})
                entry["lastRole"] = current_role
        except Exception:
            pass
    _save_fame_history(history)


def _last_period_keys_from_history(history: dict, max_periods: int = 2) -> list[str]:
    """Descobre as últimas chaves de corrida (raceKey) ativas ordenando por timestamp dos eventos de timeline.
    Útil para identificar a corrida atual/mais recente e a anterior, mesmo se as chaves não forem ordenáveis lexicograficamente.
    """
    try:
        race_to_ts = {}
        players = (history or {}).get("players", {})
        for _tag, pdata in players.items():
            for ev in pdata.get("timeline", []) or []:
                rk = ev.get("raceKey")
                ts = ev.get("ts")
                if not rk or not ts:
                    continue
                try:
                    tsv = datetime.fromisoformat(ts.replace('Z','+00:00')).timestamp()
                except Exception:
                    continue
                prev = race_to_ts.get(rk)
                if prev is None or tsv > prev:
                    race_to_ts[rk] = tsv
        if not race_to_ts:
            # fallback: usa lastRaceKey se existir
            last_rk = ((history or {}).get("meta") or {}).get("lastRaceKey")
            return [last_rk] if last_rk else []
        # ordena por ts desc e pega os últimos N
        ordered = sorted(race_to_ts.items(), key=lambda kv: -kv[1])
        return [rk for rk, _ts in ordered[:max_periods]]
    except Exception:
        return []


def _compute_danger_list(members: list) -> list[dict]:
    """Calcula zona de perigo de expulsão.
    Critério: membros com >=10 dias no clã e cuja soma de medalhas nas últimas 2 corridas
    (mais recentes ativas) esteja < 30% da média do clã para esse mesmo intervalo.
    Retorna lista ordenada pela soma ascendente.
    """
    try:
        history = _load_fame_history()
        players_hist = (history or {}).get("players", {})
        period_keys = _last_period_keys_from_history(history, 2)
        if not period_keys:
            return []
        now = datetime.now(timezone.utc)
        # calcula soma por membro e média de referência
        eligible = []
        sums = []
        for m in members or []:
            tag = m.get("tag")
            if not tag:
                continue
            first_seen = None
            try:
                ph = players_hist.get(tag, {})
                fs = ph.get("firstSeen") or m.get("firstSeen")
                if fs:
                    first_seen = datetime.fromisoformat(fs.replace('Z','+00:00'))
            except Exception:
                first_seen = None
            days = None
            if first_seen:
                try:
                    days = (now - first_seen).total_seconds() / 86400.0
                except Exception:
                    days = None
            # precisa ter >= 10 dias no clã
            if days is None or days < 10:
                continue
            byp = (players_hist.get(tag, {}) or {}).get("by_period", {})
            two_sum = 0
            for rk in period_keys:
                two_sum += int((byp.get(rk) or {}).get("total", 0) or 0)
            entry = {
                "name": m.get("name"),
                "tag": tag,
                "role": m.get("role"),
                "trophies": m.get("trophies"),
                "daysInClan": round(days, 1),
                "twoPeriodSum": int(two_sum),
                "periodKeys": period_keys,
                "firstSeen": (players_hist.get(tag, {}) or {}).get("firstSeen") or m.get("firstSeen")
            }
            eligible.append(entry)
            sums.append(two_sum)
        if not eligible:
            return []
        avg = (sum(sums) / max(1, len(sums))) if sums else 0.0
        threshold = avg * 0.30
        danger = [e for e in eligible if e.get("twoPeriodSum", 0) < threshold]
        # ordena pela soma ascendente (menores primeiro)
        danger.sort(key=lambda x: (x.get("twoPeriodSum", 0), x.get("trophies") or 0))
        # inclui metadado de referência para uso no front se quiser exibir
        for e in danger:
            e["referenceAvg"] = int(round(avg))
            e["threshold"] = int(round(threshold))
        return danger
    except Exception:
        return []


def _compute_promotion_list(members: list) -> list[dict]:
    """Calcula zona de promoção.
    Critério: membros com >4 dias no clã e cuja soma de medalhas nas últimas 2 corridas
    (mais recentes ativas) esteja >= 150% da média do clã para esse mesmo intervalo.
    Retorna lista ordenada pela soma descendente.
    """
    try:
        history = _load_fame_history()
        players_hist = (history or {}).get("players", {})
        period_meta = (history or {}).get("periodMeta", {})
        period_keys = _last_period_keys_from_history(history, 2)
        if not period_keys:
            return []
        now = datetime.now(timezone.utc)

        eligible = []
        sums = []
        for m in members or []:
            tag = m.get("tag")
            if not tag:
                continue
            # exclui líderes e colíderes da promoção
            try:
                role_key = str(m.get("role") or "").lower()
                if role_key in {"leader", "coleader"}:
                    continue
            except Exception:
                pass
            # dias no clã
            first_seen = None
            try:
                ph = players_hist.get(tag, {})
                fs = ph.get("firstSeen") or m.get("firstSeen")
                if fs:
                    first_seen = datetime.fromisoformat(fs.replace('Z','+00:00'))
            except Exception:
                first_seen = None
            days = None
            if first_seen:
                try:
                    days = (now - first_seen).total_seconds() / 86400.0
                except Exception:
                    days = None
            if days is None or days <= 4:
                continue

            # participação em guerras (exclui training) e soma 2 corridas
            byp = (players_hist.get(tag, {}) or {}).get("by_period", {})
            # exige no mínimo 3 guerras com contribuição (>0), ignorando periods de treinamento
            wars_count = 0
            try:
                for rk, v in (byp or {}).items():
                    meta = (period_meta.get(rk) or {})
                    ptype = str(meta.get("periodType") or "").lower()
                    if ptype == "training":
                        continue
                    tot_v = int((v or {}).get("total", 0) or 0)
                    if tot_v > 0:
                        wars_count += 1
            except Exception:
                wars_count = 0
            if wars_count < 3:
                continue

            # soma nas últimas 2 corridas mais recentes
            two_sum = 0
            for rk in period_keys:
                two_sum += int((byp.get(rk) or {}).get("total", 0) or 0)
            entry = {
                "name": m.get("name"),
                "tag": tag,
                "role": m.get("role"),
                "trophies": m.get("trophies"),
                "daysInClan": round(days, 1),
                "twoPeriodSum": int(two_sum),
                "periodKeys": period_keys,
                "firstSeen": (players_hist.get(tag, {}) or {}).get("firstSeen") or m.get("firstSeen")
            }
            eligible.append(entry)
            sums.append(two_sum)

        if not eligible:
            return []
        avg = (sum(sums) / max(1, len(sums))) if sums else 0.0
        threshold = avg * 1.50
        top = [e for e in eligible if e.get("twoPeriodSum", 0) >= threshold]
        # ordena por soma descendente (maiores primeiro)
        top.sort(key=lambda x: (-x.get("twoPeriodSum", 0), -(x.get("trophies") or 0)))
        for e in top:
            e["referenceAvg"] = int(round(avg))
            e["threshold"] = int(round(threshold))
        return top
    except Exception:
        return []

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


def _sync_player_histories(members: list) -> None:
    """Sincroniza arquivos historico_{tag}.json para membros atuais e remove de ex‑membros.

    - Para cada membro atual, cria/atualiza o arquivo com as batalhas mais recentes
      (evitando duplicados, mesma lógica de /api/historico).
    - Para quaisquer arquivos existentes de jogadores que não estejam mais no clã,
      remove o arquivo para manter apenas atuais.
    """
    try:
        current_tags = []
        for m in members or []:
            mt = m.get("tag")
            if not mt:
                continue
            current_tags.append(mt)
            safe = re.sub(r"\W+", "", mt)
            arquivo = f"historico_{safe}.json"

            # Carrega histórico existente
            historico = []
            if os.path.exists(arquivo):
                try:
                    with open(arquivo, "r", encoding="utf-8") as f:
                        historico = json.load(f)
                except Exception:
                    historico = []

            # Busca batalhas recentes e agrega não vistos
            try:
                batalhas = fetch_batalhas(mt, tipos=None)
            except Exception:
                batalhas = []
            try:
                seen = {h.get("battleTime", "") + ((h.get("team") or [{}])[0].get("tag") or "") for h in historico}
                novas = [b for b in (batalhas or []) if (b.get("battleTime", "") + ((b.get("team") or [{}])[0].get("tag") or "")) not in seen]
            except Exception:
                novas = []

            if novas:
                try:
                    historico += novas
                    with open(arquivo, "w", encoding="utf-8") as f:
                        json.dump(historico, f, ensure_ascii=False, indent=4)
                except Exception:
                    pass

        # Remoção de históricos de ex‑membros
        try:
            current_safe = {re.sub(r"\W+", "", t) for t in current_tags}
            files = [f for f in os.listdir() if f.startswith("historico_") and f.endswith(".json")]
            for fpath in files:
                try:
                    tag_clean = re.sub(r"^historico_|\.json$", "", fpath)
                    if tag_clean and tag_clean not in current_safe:
                        try:
                            os.remove(fpath)
                        except Exception:
                            pass
                except Exception:
                    continue
        except Exception:
            pass
    except Exception:
        # Falhas não devem quebrar o scheduler
        pass

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/tool")
def tool():
    tag = request.args.get("tag", "")
    return render_template("tool.html", player_tag=tag)

@app.route("/api/clan-info")
def clan_info():
    tag_enc = CLAN_TAG.replace("#", "%23")
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

    # Enriquecer com 'fame' da guerra atual (River Race)
    fame_by_tag = {}
    try:
        rr_url = f"https://api.clashroyale.com/v1/clans/{tag_enc}/currentriverrace"
        rr_resp = requests.get(rr_url, headers=get_headers())
        if rr_resp.status_code == 200:
            rr = rr_resp.json()
            # Estrutura esperada: { "clan": { "participants": [ { tag, fame, ... } ] } }
            participants = (
                (rr.get("clan") or {}).get("participants")
                or rr.get("participants")
                or []
            )
            fame_by_tag = {p.get("tag"): p.get("fame", 0) for p in participants if p.get("tag")}
            # Atualiza histórico a partir da corrida atual
            try:
                totals, rank_delta, race_key = _update_fame_history(rr, participants)
            except Exception:
                totals, rank_delta, race_key = {}, {}, None
            # Também atualiza firstSeen/lastSeen a partir da lista de membros atual
            try:
                _update_members_first_seen(members)
            except Exception:
                pass
        else:
            rr = {}
            totals, rank_delta, race_key = {}, {}, None
    except Exception:
        # Se falhar, apenas segue sem fame
        fame_by_tag = {}
        rr = {}
        totals, rank_delta, race_key = {}, {}, None

    # Carrega histórico para computar valores da guerra anterior
    try:
        history_all = _load_fame_history()
        last_two = _last_period_keys_from_history(history_all, 2)
        prev_key = last_two[1] if len(last_two) > 1 else None
        hist_players_all = (history_all or {}).get("players", {})
    except Exception:
        prev_key = None
        hist_players_all = {}

    for m in members:
        try:
            m["fame"] = fame_by_tag.get(m.get("tag"), 0)
        except Exception:
            m["fame"] = 0
        # totais cumulativos (podem não existir se histórico não conseguiu atualizar)
        m["fameTotal"] = (totals.get(m.get("tag")) if 'totals' in locals() else None) or 0
        m["rankDelta"] = (rank_delta.get(m.get("tag")) if 'rank_delta' in locals() else None) or 0
        # doações semanais vindas do endpoint de membros
        try:
            m["donations"] = int(m.get("donations") or 0)
        except Exception:
            m["donations"] = 0
        # medalhas da guerra anterior
        try:
            if prev_key:
                ph = hist_players_all.get(m.get("tag"), {}) or {}
                byp = ph.get("by_period", {}) or {}
                m["famePrev"] = int(((byp.get(prev_key) or {}).get("total", 0)) or 0)
            else:
                m["famePrev"] = 0
        except Exception:
            m["famePrev"] = 0
        # firstSeen
        try:
            hist_players = _load_fame_history().get("players", {})
            player_hist = (hist_players.get(m.get("tag"), {}) or {})
            m["firstSeen"] = player_hist.get("firstSeen")
            # seta persistente por 24h após a última mudança
            last_change = player_hist.get("rankLastChangeTs")
            last_dir = player_hist.get("rankLastDelta")  # 1 up, -1 down
            if last_change and last_dir:
                try:
                    ts = datetime.fromisoformat(last_change.replace('Z','+00:00'))
                    hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
                    if hours < 24:
                        m["rankDeltaSticky"] = 1 if int(last_dir) > 0 else -1
                    else:
                        m["rankDeltaSticky"] = 0
                except Exception:
                    m["rankDeltaSticky"] = 0
            else:
                m["rankDeltaSticky"] = 0
        except Exception:
            m["firstSeen"] = None
            m["rankDeltaSticky"] = 0

    # Calcula lista de perigo de expulsão (sempre independente do filtro visual do front)
    try:
        danger_list = _compute_danger_list(members)
    except Exception:
        danger_list = []
    # Calcula lista de promoção
    try:
        promotion_list = _compute_promotion_list(members)
    except Exception:
        promotion_list = []

    return jsonify({
        "clan": {
            "name": info.get("name"),
            "tag": info.get("tag"),
            "membersCount": info.get("members"),
            "clanScore": info.get("clanScore"),
            "clanWarTrophies": info.get("clanWarTrophies"),
            "badgeUrl": badge_url,
            # Campo de criação do clã (usa valor da API, senão usa data fixa solicitada)
            "createdAt": (
                info.get("createdDate")
                or info.get("creationDate")
                or info.get("createdTime")
                or info.get("created")
                or "2025-09-15T00:00:00Z"
            ),
            "riverRace": {
                "periodType": (rr.get("periodType") if 'rr' in locals() else None),
                "sectionIndex": (rr.get("sectionIndex") if 'rr' in locals() else None),
                "periodIndex": (rr.get("periodIndex") if 'rr' in locals() else None),
                "seasonId": (rr.get("seasonId") if 'rr' in locals() else None)
            }
        },
        "memberList": [
            {
                "name": m.get("name"),
                "tag": m.get("tag"),
                "role": m.get("role"),
                "trophies": m.get("trophies"),
                "fame": m.get("fame", 0),
                "famePrev": m.get("famePrev", 0),
                "fameTotal": m.get("fameTotal", 0),
                "rankDelta": m.get("rankDelta", 0),
                "rankDeltaSticky": m.get("rankDeltaSticky", 0),
                "firstSeen": m.get("firstSeen"),
                "donations": m.get("donations", 0)
            }
            for m in members
        ],
        "dangerList": danger_list,
        "promotionList": promotion_list
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


@app.route("/api/player")
def api_player():
    tag = request.args.get("tag", "").strip()
    if not tag:
        return jsonify({"error": "tag requerida"}), 400
    tag_enc = tag.replace("#", "%23")
    url = f"https://api.clashroyale.com/v1/players/{tag_enc}"
    try:
        resp = requests.get(url, headers=get_headers())
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return jsonify({"error": "falha ao buscar player", "detail": str(e)}), 500
    # extrai deck atual (se existir)
    deck = data.get("currentDeck") or data.get("cards") or []
    # atualiza histórico de deck e winrate por deck
    try:
        hist = _load_fame_history()
        players = hist.setdefault("players", {})
        p = players.setdefault(tag, {"name": data.get("name"), "total": 0, "by_period": {}, "timeline": []})
        dk = _deck_key(deck)
        now = _now_iso()
        dh = p.setdefault("deckHistory", {})
        curr = dh.get("current", {})
        if curr.get("key") != dk:
            # troca de deck: arquiva e inicia novo
            if curr.get("key"):
                past = p.setdefault("pastDecks", [])
                past.append(curr)
            dh["current"] = {"key": dk, "since": now, "wins": 0, "losses": 0}
        # nota: wins/losses alimentados pelo updater de partidas quando disponível
        _save_fame_history(hist)
    except Exception:
        pass
    # agrega win/loss do deck atual se existir no histórico
    deck_stats = None
    try:
        hist = _load_fame_history()
        p = hist.get("players", {}).get(tag)
        curr = (p or {}).get("deckHistory", {}).get("current") or {}
        if curr.get("key"):
            deck_stats = {"wins": int(curr.get("wins") or 0), "losses": int(curr.get("losses") or 0), "since": curr.get("since")}
    except Exception:
        deck_stats = None

    return jsonify({
        "name": data.get("name"),
        "tag": data.get("tag"),
        "trophies": data.get("trophies"),
        "league": (data.get("leagueStatistics") or {}).get("currentSeason"),
        "currentDeck": deck,
        "deckStats": deck_stats
    })


@app.route("/api/player-fame")
def api_player_fame():
    tag = request.args.get("tag", "").strip()
    if not tag:
        return jsonify({"error": "tag requerida"}), 400
    hist = _load_fame_history()
    p = hist.get("players", {}).get(tag)
    periods = hist.get("periodMeta", {})
    if not p:
        return jsonify({"periods": [], "totals": {}})
    byp = p.get("by_period", {})
    # mapeia para lista ordenada por chave para front
    items = []
    for k, v in byp.items():
        meta = periods.get(k, {})
        items.append({"raceKey": k, "total": v.get("total", 0), "meta": meta})
    items.sort(key=lambda x: x["raceKey"])  # simples
    return jsonify({
        "periods": items,
        "totalCumulative": p.get("total", 0),
        "roleTimeline": p.get("roleTimeline", []),
        "winRateTimeline": p.get("winRateTimeline", [])
    })


@app.route("/api/player-matches")
def api_player_matches():
    tag = request.args.get("tag", "").strip()
    if not tag:
        return jsonify({"error": "tag requerida"}), 400
    tag_enc = tag.replace("#", "%23")
    url = f"https://api.clashroyale.com/v1/players/{tag_enc}/battlelog"
    try:
        resp = requests.get(url, headers=get_headers())
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return jsonify({"error": "falha ao buscar partidas", "detail": str(e)}), 500
    # retornamos as últimas N partidas (a API geralmente entrega ~25-50)
    return jsonify(data)

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

    # Inicia atualizador em background (evita rodar duas vezes no reloader)
    def _bg_updater():
        while True:
            try:
                tag_enc_local = CLAN_TAG.replace("#", "%23")
                rr_url = f"https://api.clashroyale.com/v1/clans/{tag_enc_local}/currentriverrace"
                rr_resp = requests.get(rr_url, headers=get_headers())
                if rr_resp.status_code == 200:
                    rr = rr_resp.json()
                    participants = ((rr.get("clan") or {}).get("participants") or rr.get("participants") or [])
                    # Atualiza histórico
                    _update_fame_history(rr, participants)
                    # Atualiza firstSeen/lastSeen capturando membros atuais
                    members_url_local = f"https://api.clashroyale.com/v1/clans/{tag_enc_local}/members"
                    m_resp = requests.get(members_url_local, headers=get_headers())
                    if m_resp.status_code == 200:
                        m_json = m_resp.json()
                        members_local = m_json.get("memberList") or m_json.get("items") or []
                        _update_members_first_seen(members_local)
                        # Sincroniza históricos por jogador (cria/atualiza e remove ex‑membros)
                        try:
                            _sync_player_histories(members_local)
                        except Exception:
                            pass
                    # Atualiza winrate por deck para membros com partidas recentes
                    try:
                        hist = _load_fame_history()
                        players = hist.setdefault("players", {})
                        for m in (members_local or []):
                            mt = m.get("tag")
                            if not mt:
                                continue
                            # pega partidas
                            try:
                                pl = requests.get(f"https://api.clashroyale.com/v1/players/{mt.replace('#','%23')}/battlelog", headers=get_headers())
                                if pl.status_code==200:
                                    bl = pl.json()
                                    # usa últimas 50 para janela
                                    bl = bl[:50]
                                    # tenta identificar deck do time
                                    p = players.setdefault(mt, {"name": m.get("name"), "total": 0, "by_period": {}, "timeline": []})
                                    dh = p.setdefault("deckHistory", {})
                                    curr = dh.get("current") or {}
                                    key = curr.get("key") or ""
                                    if key:
                                        w,l=0,0
                                        for b in bl:
                                            team = (b.get('team') or [{}])[0]
                                            opp = (b.get('opponent') or [{}])[0]
                                            dkey = _cards_key(team.get('cards') or [])
                                            if dkey==key:
                                                if (team.get('crowns') or 0)>(opp.get('crowns') or 0): w+=1
                                                else: l+=1
                                        curr['wins']=w; curr['losses']=l
                                        dh['current']=curr
                                    players[mt]=p
                                    # snapshot de winrate geral (todas partidas)
                                    try:
                                        cw,cl=0,0
                                        for b in bl:
                                            team = (b.get('team') or [{}])[0]
                                            opp = (b.get('opponent') or [{}])[0]
                                            if (team.get('crowns') or 0)>(opp.get('crowns') or 0): cw+=1
                                            else: cl+=1
                                        wr = (cw/(cw+cl))*100 if (cw+cl)>0 else 0
                                        tl = p.setdefault('winRateTimeline', [])
                                        tl.append({'ts': _now_iso(), 'wr': round(wr,2), 'n': cw+cl})
                                        # limita tamanho
                                        if len(tl)>720:
                                            del tl[:len(tl)-720]
                                    except Exception:
                                        pass
                                    _save_fame_history(hist)
                            except Exception:
                                continue
                    except Exception:
                        pass
            except Exception as e:
                try:
                    app.logger.warning(f"Atualizador de fama falhou: {e}")
                except Exception:
                    pass
            time.sleep(60*60)  # a cada 1h

    try:
        import os as _os
        if _os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
            t = threading.Thread(target=_bg_updater, daemon=True)
            t.start()
    except Exception:
        pass

    # Atualiza imediatamente na inicialização para que a página já traga dados frescos
    try:
        tag_enc_local = CLAN_TAG.replace("#", "%23")
        rr_url = f"https://api.clashroyale.com/v1/clans/{tag_enc_local}/currentriverrace"
        rr_resp = requests.get(rr_url, headers=get_headers())
        if rr_resp.status_code == 200:
            rr = rr_resp.json()
            participants = ((rr.get("clan") or {}).get("participants") or rr.get("participants") or [])
            _update_fame_history(rr, participants)
            members_url_local = f"https://api.clashroyale.com/v1/clans/{tag_enc_local}/members"
            m_resp = requests.get(members_url_local, headers=get_headers())
            if m_resp.status_code == 200:
                m_json = m_resp.json()
                members_local = m_json.get("memberList") or m_json.get("items") or []
                _update_members_first_seen(members_local)
                try:
                    _sync_player_histories(members_local)
                except Exception:
                    pass
    except Exception:
        pass

    app.run(host="0.0.0.0", port=5001, debug=True)

