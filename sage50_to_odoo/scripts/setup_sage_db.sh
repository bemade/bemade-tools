#!/usr/bin/env bash
#
# Turn a Sage 50 Canadian Edition backup (.CAB) into a queryable MySQL server.
#
#   ./setup_sage_db.sh /path/to/backup.cab [workdir]
#
# Sage 50 CA stores its company data in a MySQL 8.0 data directory: the
# `<company>.SAJ` folder IS the datadir, and the `.CAB` backup is a plain
# Microsoft Cabinet archive holding it plus the small `.SAI` companion.
# So no vendor tooling, no Sage licence and no Windows box are needed --
# extract the cabinet, point a matching mysqld at the datadir, read.
#
# Everything runs in userland: the MySQL tarball is unpacked into the work
# directory and the server listens on a unix socket only. Nothing is
# installed system-wide and the client's data never leaves the work dir.
#
# READ ONLY. Never write to this database -- Sage keeps no referential
# integrity and hands out record ids from internal counters, so a write
# corrupts the file in ways Sage will not notice until much later.
set -euo pipefail

CAB="${1:?usage: setup_sage_db.sh <backup.cab> [workdir]}"
ROOT="${2:-$(cd "$(dirname "$CAB")" && pwd)}"

# Match the server version that wrote the datadir, or InnoDB will refuse to
# open it (or silently upgrade the data dictionary in place). Sage 50 2024-2026
# ships Oracle MySQL 8.0.27 -- confirm against errorlog.txt inside the .SAJ.
MYSQL_VER=8.0.27
MYSQL_DIR="mysql-${MYSQL_VER}-linux-glibc2.17-x86_64-minimal"
MYSQL_TGZ="${MYSQL_DIR}.tar.xz"
MYSQL_URL="https://cdn.mysql.com/archives/mysql-8.0/${MYSQL_TGZ}"

cd "$ROOT"
mkdir -p extract sagedb/{data,tmp,run} lib

echo "==> extracting cabinet"
command -v cabextract >/dev/null || { echo "need: sudo apt install cabextract" >&2; exit 1; }
cabextract -q -d extract "$CAB"

SAJ="$(find extract -maxdepth 1 -type d -name '*.SAJ' | head -1)"
[ -n "$SAJ" ] || { echo "no .SAJ directory in the cabinet" >&2; exit 1; }
echo "    datadir: $SAJ"
echo "    server that wrote it:"
grep -o "mysqld [0-9.]*-[a-z]*" "$SAJ/errorlog.txt" | tail -1 | sed 's/^/      /'

echo "==> fetching mysqld ${MYSQL_VER}"
[ -d "$MYSQL_DIR" ] || { curl -sS -o "$MYSQL_TGZ" "$MYSQL_URL" && tar -xf "$MYSQL_TGZ"; }
MB="$ROOT/$MYSQL_DIR"

# The minimal tarball links libaio.so.1; Ubuntu 24.04 ships the t64 ABI
# rename. A private symlink keeps this out of the system library path.
if [ -e /usr/lib/x86_64-linux-gnu/libaio.so.1t64 ]; then
  ln -sf /usr/lib/x86_64-linux-gnu/libaio.so.1t64 lib/libaio.so.1
fi
export LD_LIBRARY_PATH="$ROOT/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

echo "==> staging datadir (the extract stays pristine)"
cp -a "$SAJ/." sagedb/data/

cat > sagedb/my.cnf <<EOF
[mysqld]
basedir=$MB
datadir=$ROOT/sagedb/data
socket=$ROOT/sagedb/run/mysql.sock
pid-file=$ROOT/sagedb/run/mysqld.pid
tmpdir=$ROOT/sagedb/tmp
log-error=$ROOT/sagedb/run/error.log
bind-address=127.0.0.1
# The datadir carries Sage's own users, whose passwords we do not have.
# Skipping grants also implies --skip-networking, so the socket is the
# only way in -- which is exactly what we want for client data.
skip-grant-tables
# Written on Windows, so names are stored folded. MySQL 8 records this in
# the data dictionary and refuses to start if the server disagrees.
lower_case_table_names=1
innodb_log_file_size=8388608
innodb_buffer_pool_size=1G
secure_file_priv=$ROOT/sagedb/tmp
[client]
socket=$ROOT/sagedb/run/mysql.sock
EOF

echo "==> starting mysqld"
"$MB/bin/mysqld" --defaults-file="$ROOT/sagedb/my.cnf" --daemonize
sleep 2
tail -1 sagedb/run/error.log

cat > sage-mysql.sh <<EOF
#!/usr/bin/env bash
# start | stop | dump  -- see tools/sage/README.md
set -euo pipefail
ROOT="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
MB="\$ROOT/$MYSQL_DIR"
export LD_LIBRARY_PATH="\$ROOT/lib\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
CNF="\$ROOT/sagedb/my.cnf"
case "\${1:-}" in
  start) "\$MB/bin/mysqld" --defaults-file="\$CNF" --daemonize ;;
  stop)  "\$MB/bin/mysqladmin" --defaults-file="\$CNF" shutdown ;;
  dump)  shift; "\$MB/bin/mysqldump" --defaults-file="\$CNF" -u root "\$@" ;;
  *) echo "usage: \$0 {start|stop|dump}" >&2; exit 2 ;;
esac
EOF
chmod +x sage-mysql.sh

echo
echo "ready. socket: $ROOT/sagedb/run/mysql.sock  schema: simply"
echo "  archive dump: ./sage-mysql.sh dump --no-tablespaces --skip-lock-tables --databases simply > simply-full.sql"
