#!/usr/bin/env bash
# Сторож П-10 ОРВ — один проход проверки; запускает таймер p10-watchdog.timer раз в минуту.
#
# systemd сам перезапускает упавший процесс, но не замечает зависшего: процесс жив,
# а страница не отвечает, или туннель жив, а ссылка снаружи не открывается. Это ловит сторож:
#   1. сервис — страница входа на 127.0.0.1:8030 отвечает 200;
#   2. ссылка — https://p10-orv.ru.tuna.am/login отвечает 200 (проверяется, только если
#      сервис жив: при лежащем сервисе туннель отдаёт ошибку не по своей вине).
# Три провала подряд (около трёх минут) — перезапуск нужного юнита. Провалы, перезапуски
# и восстановления пишутся в var/logs/watchdog.log; при здоровом сервисе сторож молчит.
set -u

LOG=/home/masked/projects/p10-orv/var/logs/watchdog.log
STATE="${XDG_RUNTIME_DIR:-/tmp}/p10-watchdog"   # счётчики провалов подряд, живут до перезагрузки
FAILS_TO_RESTART=3
mkdir -p "$STATE" "$(dirname "$LOG")"

say() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

# check <имя> <адрес> <юнит> — 0, если ответ 200; иначе счёт провалов и, на третьем, перезапуск
check() {
    local name=$1 url=$2 unit=$3 code n
    code=$(curl -s -o /dev/null -m 15 -w '%{http_code}' "$url")
    if [ "$code" = "200" ]; then
        if [ -f "$STATE/$name" ]; then
            say "$name: снова работает"
            rm -f "$STATE/$name"
        fi
        return 0
    fi
    n=$(( $(cat "$STATE/$name" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$STATE/$name"
    say "$name: ответ $code ($url), провал $n подряд"
    if [ "$n" -ge "$FAILS_TO_RESTART" ]; then
        say "$name: перезапускаю $unit"
        systemctl --user restart "$unit"
        echo 0 > "$STATE/$name"   # счёт заново; файл остаётся, чтобы записать «снова работает»
    fi
    return 1
}

check "сервис" "http://127.0.0.1:8030/login" "p10-web.service" &&
    check "ссылка" "https://p10-orv.ru.tuna.am/login" "p10-tuna.service"
exit 0
