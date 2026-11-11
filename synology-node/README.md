# 3x-ui + Tailscale node (Synology / Docker)

Один контейнер: **3x-ui (панель + Xray)** и **Tailscale-клиент** в userspace-режиме.
Весь трафик уходит на exit node VPS (`62yun-vps`, `100.116.29.54`); наружу открываются
только VLESS/REALITY и (опционально) подписки. Все настройки — в `config.yaml`,
который монтируется в контейнер.

```
Клиент → [Synology: 3x-ui] → Tailscale userspace SOCKS → exit node VPS → Internet
```

## Файлы
| Файл | Назначение |
|---|---|
| `Dockerfile` | 3x-ui + tailscale binaries |
| `configure.py` | декларативная настройка панели, inbounds, Xray, Tailscale |
| `entrypoint.sh` | порядок запуска |
| `config.example.yaml` | шаблон конфигурации |
| `docker-compose.yml` | запуск на Synology |

## Готовый образ
GitHub Actions собирает multi-arch образ при пуше в ветку `synology-node`:
```
ghcr.io/grifonsk/xray-node:latest
```
Если пакет приватный — выполните на NAS `docker login ghcr.io` (или сделайте пакет публичным).

## Быстрый старт
1. Скопируйте `config.example.yaml` → `config.yaml` и заполните:
   - `tailscale.auth_key` — ключ из Tailscale Admin → Settings → Keys;
   - `tailscale.exit_node` — `100.116.29.54` (VPS должен быть **approved exit node**);
   - `vless.share_domain` и `subscription.domain` — публичный домен/IP этой площадки;
   - `reality.target` / `server_names` — сайт с TLS 1.3, правдоподобный для этой площадки;
   - `panel.password`.
2. Соберите:
   ```bash
   docker compose build
   ```
3. Запустите:
   ```bash
   docker compose up -d
   ```
4. Смотрите логи — там будут готовые ссылки и URL подписок:
   ```bash
   docker logs -f xray-node
   ```
5. На роутере пробросьте:
   - `10890/tcp` → Synology:10890 (VLESS)
   - `10889/tcp` → Synology:10889 (подписки, если нужны)
   - `2053/tcp` — только LAN (панель).

## Synology
- Container Manager → **Project** → создать из папки с `docker-compose.yml`
  (или по SSH: `sudo docker compose up -d --build`).
- Архитектура образа должна совпадать с NAS (amd64/arm64). Проще собирать
  прямо на NAS; либо собрать на такой же машине и перенести `docker save`/`load`.

## TLS для панели/подписок
- Положите сертификаты в `./certs`, раскомментируйте volume в compose и
  `cert_file`/`key_file` в `config.yaml`.
- После этого URL подписки станет `https://...:10889/...`.

## Важно
- Логин/пароль панели применяются из `config.yaml` **при каждом старте**.
- REALITY-ключи и UUID генерируются один раз и хранятся в `/etc/x-ui/node-state.json`.
- База 3x-ui: `./data/x-ui` (делайте бэкап).
- Состояние Tailscale: `./data/tailscale` (удаление = повторная регистрация узла).
- Если площадка в России — включите split routing:
  ```yaml
  routing:
    direct_geosite: ["geosite:category-ru"]
    direct_geoip: ["geoip:ru"]
  ```
  Иначе оставьте списки пустыми — весь трафик пойдёт через VPS.
- MSS-клампинг опционален; без `NET_ADMIN` он просто пропускается (можно
  настроить на самом Synology/роутере).
- `xray.outbound`: `tailscale-socks` (exit node VPS, рекомендуется),
  `vps-socks` (SOCKS 3x-ui на VPS через Tailscale), `direct` (без VPS, для теста).
