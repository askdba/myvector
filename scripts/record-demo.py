#!/usr/bin/env python3
"""Record the self-playing demo on the docs site (docs/DEMO.md).

Runs every step of the demo for real against a fresh MyVector Docker container
and writes the session as an asciicast v2 file, which asciinema-player replays
on the page. The output in the recording is the real output of each command,
including MySQL's own "(0.00 sec)" timings. Only the typing is simulated, and
long waits are shortened (as asciinema's idle_time_limit would).

Usage:
    python3 scripts/record-demo.py                       # default image
    python3 scripts/record-demo.py --image ghcr.io/askdba/myvector:mysql9.7-component \
        --output docs/assets/demo/myvector-demo.cast

Requires Docker and examples/stanford50d/insert50d.sql.gz. Takes about
five minutes, most of it loading the word vectors.

The demo is written for component images up to v1.26.9: vector columns are
declared with a 'MYVECTOR COLUMN' comment and searches use myvector_ann_set().
"""

import argparse
import json
import pathlib
import random
import subprocess
import sys
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
CONTAINER = "myvector-demo"
ROOT_PW = "myvector"
DB = "demo"
INDEX = f"{DB}.words50d.wordvec"
WORDS = 50000  # GloVe is sorted by frequency: the 50,000 most common words

WIDTH, HEIGHT = 112, 32

# ANSI styles
RESET = "\x1b[0m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
GREEN = "\x1b[32m"
CYAN = "\x1b[36m"
YELLOW = "\x1b[33m"


class Cast:
    """Collects asciicast v2 events on a synthetic clock."""

    def __init__(self, seed=7):
        self.t = 0.0
        self.events = []
        self.rng = random.Random(seed)
        self.typed = set()  # statements typed before are replayed faster

    def out(self, text):
        self.events.append([round(self.t, 3), "o", text.replace("\n", "\r\n")])

    def pause(self, seconds):
        self.t += seconds

    def marker(self, label):
        self.events.append([round(self.t, 3), "m", label])

    def type(self, text, fast=False):
        for ch in text:
            self.out(ch)
            delay = self.rng.uniform(0.025, 0.06) if ch != " " else 0.07
            self.pause(delay / 6 if fast else delay)

    def comment(self, text, prompt):
        """A narration line, typed at the prompt as a shell or SQL comment."""
        self.out(prompt)
        self.pause(0.3)
        lead = "# " if prompt == SHELL_PROMPT else "-- "
        self.out(DIM + YELLOW)
        self.type(lead + text)
        self.out(RESET + "\n")
        self.pause(0.6)

    def output(self, text, line_delay=0.02):
        for line in text.rstrip("\n").split("\n"):
            self.out(line + "\n")
            self.pause(line_delay)


SHELL_PROMPT = f"{BOLD}{GREEN}${RESET} "
MYSQL_PROMPT = f"{BOLD}{CYAN}mysql>{RESET} "
MYSQL_CONT = f"{BOLD}{CYAN}    ->{RESET} "


def run(cmd, check=True, **kw):
    res = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if check and res.returncode != 0:
        sys.exit(f"command failed: {cmd}\n{res.stdout}\n{res.stderr}")
    return res


def shell(cast, display, actual=None, show_output=True, typing=True):
    """Type a shell command, run it for real, show its output."""
    cast.out(SHELL_PROMPT)
    cast.pause(0.3)
    if typing:
        cast.type(display)
    else:
        cast.out(display)
    cast.out("\n")
    cast.pause(0.2)
    res = run(actual if actual is not None else ["bash", "-c", display], cwd=REPO)
    if show_output and res.stdout.strip():
        cast.output(res.stdout)
    cast.pause(0.8)
    return res.stdout


def mysql_session(cast, steps):
    """Run a list of steps in one mysql session and replay them.

    Each step is ("--", comment), ("chapter", label), ("vertical", statement)
    or a SQL statement. Statements run in a single real session (so @variables
    carry over) with -vvv, which prints each statement's result and timing the
    way the interactive client does. The client does not accept \\G in batch
    mode, so a ("vertical", ...) step is shown with \\G but runs on its own
    with --vertical; @variables do not carry across it.
    """
    for i, step in enumerate(steps):
        if isinstance(step, tuple) and step[0] == "vertical":
            if steps[:i]:
                _replay(cast, steps[:i])
            _replay(cast, [step[1]], vertical=True)
            if steps[i + 1:]:
                mysql_session(cast, steps[i + 1:])
            return
    _replay(cast, steps)


def _replay(cast, steps, vertical=False):
    sql = [s for s in steps if not isinstance(s, tuple)]
    res = run(
        ["docker", "exec", "-i", "-e", f"MYSQL_PWD={ROOT_PW}", CONTAINER,
         "mysql", "-uroot", "-vvv", "--vertical" if vertical else "--table", DB],
        input="\n".join(s + ";" if vertical else s for s in sql) + "\n",
    )
    if res.stderr.strip():
        sys.exit(f"mysql reported errors:\n{res.stderr}")
    # -vvv output: "----\n<stmt>\n----\n<result>" per statement, then "Bye".
    parts = res.stdout.split("--------------\n")
    results = [parts[i] for i in range(2, len(parts), 2)]
    if len(results) != len(sql):
        sys.exit(f"expected {len(sql)} results, got {len(results)}:\n{res.stdout}")

    it = iter(results)
    for step in steps:
        if isinstance(step, tuple):
            if step[0] == "chapter":
                chapter(cast, step[1])
            else:
                cast.comment(step[1], MYSQL_PROMPT)
            continue
        lines = (step + "\\G" if vertical else step).split("\n")
        fast = step in cast.typed
        cast.typed.add(step)
        cast.out(MYSQL_PROMPT)
        cast.pause(0.3)
        for i, line in enumerate(lines):
            if i:
                cast.out(MYSQL_CONT)
            cast.type(line, fast)
            cast.out("\n")
        cast.pause(0.25)
        result = next(it).strip("\n").removesuffix("Bye").strip("\n")
        if result:
            cast.output(result)
        cast.out("\n")
        cast.pause(1.6)


def enter_mysql(cast):
    shell(cast, f"docker exec -it {CONTAINER} mysql -uroot -p{ROOT_PW} {DB}",
          actual=["true"], show_output=False)


def exit_mysql(cast):
    cast.out(MYSQL_PROMPT)
    cast.type("exit")
    cast.out("\nBye\n")
    cast.pause(0.6)


def chapter(cast, label):
    cast.marker(label)
    cast.out("\n" + BOLD + f"━━ {label} " + "━" * (WIDTH - 5 - len(label)) + RESET + "\n\n")
    cast.pause(1.0)


def ann_query(word_var, k, opts=None):
    options = f"nn={k}" + (f",{opts}" if opts else "")
    return (
        "SELECT nn.rank_no, w.word,\n"
        f"       ROUND(myvector_distance(w.wordvec, {word_var}, 'L2'), 3) AS distance\n"
        f"FROM (SELECT myvector_ann_set('{INDEX}', 'wordid', {word_var}, '{options}') AS js) src,\n"
        "     JSON_TABLE(src.js, '$[*]' COLUMNS (rank_no FOR ORDINALITY, id BIGINT PATH '$')) nn\n"
        "JOIN words50d w ON w.wordid = nn.id ORDER BY nn.rank_no;"
    )


def wait_ready():
    """Wait for the real server (not the init-time one) to accept TCP."""
    for _ in range(120):
        logs = run(["docker", "logs", CONTAINER], check=False)
        if "port: 3306" in logs.stdout + logs.stderr:
            ok = run(["docker", "exec", "-e", f"MYSQL_PWD={ROOT_PW}", CONTAINER,
                      "mysql", "-uroot", "-h127.0.0.1", "-e", "SELECT 1"], check=False)
            if ok.returncode == 0:
                return
        time.sleep(2)
    sys.exit("MySQL did not become ready")


def record(image, cast):
    cast.out(f"{BOLD}MyVector demo{RESET}: vector search inside MySQL, with {image}\n")
    cast.out(f"{DIM}Every command below ran for real; only the typing is simulated.{RESET}\n")
    cast.pause(2.5)

    # 1. start ------------------------------------------------------------
    chapter(cast, "1. Start MySQL 9.7 with the MyVector component")
    cast.comment("The init script writes myvector.cnf, which index builds and online updates use", SHELL_PROMPT)
    run(["docker", "rm", "-f", CONTAINER], check=False)
    shell(cast,
          f"docker run -d --name {CONTAINER} -e MYSQL_ROOT_PASSWORD={ROOT_PW} -e MYSQL_DATABASE={DB} \\\n"
          "    -v $PWD/scripts/demo/00-myvector-cnf.sh:/docker-entrypoint-initdb.d/00-myvector-cnf.sh:ro \\\n"
          f"    {image}",
          actual=["docker", "run", "-d", "--name", CONTAINER,
                  "-e", f"MYSQL_ROOT_PASSWORD={ROOT_PW}", "-e", f"MYSQL_DATABASE={DB}",
                  "-v", f"{REPO}/scripts/demo/00-myvector-cnf.sh:/docker-entrypoint-initdb.d/00-myvector-cnf.sh:ro",
                  image])
    cast.comment("First start initializes the data directory and installs the component (~30 s)", SHELL_PROMPT)
    wait_ready()
    shell(cast, f"docker logs {CONTAINER} 2>&1 | grep -E 'initdb.d|ready for connections.*3306' | cut -c1-110")

    # 2. installed ---------------------------------------------------------
    chapter(cast, "2. What the component adds")
    enter_mysql(cast)
    mysql_session(cast, [
        "SELECT @@version, component_urn FROM mysql.component;",
        "SELECT ROUTINE_NAME FROM information_schema.ROUTINES\n"
        "WHERE ROUTINE_SCHEMA = 'mysql' AND ROUTINE_NAME LIKE 'myvector%';",
    ])

    # 3. functions ---------------------------------------------------------
    chapter(cast, "3. Vectors and distances")
    mysql_session(cast, [
        "SELECT myvector_display(myvector_construct('[1.5, 2.5, 3.5]')) AS vec;",
        "SET @a = myvector_construct('[1, 0, 1]'), @b = myvector_construct('[0, 1, 1]');",
        "SELECT myvector_distance(@a, @b, 'L2') AS l2,\n"
        "       myvector_distance(@a, @b, 'Cosine') AS cosine,\n"
        "       myvector_distance(@a, @b, 'IP') AS ip;",
        ("--", "L2 is squared Euclidean; Cosine and IP are 1 - similarity"),
    ])

    # 4. table -------------------------------------------------------------
    chapter(cast, "4. A table with a vector column")
    mysql_session(cast, [
        ("--", "50-dimensional GloVe word vectors. online=Y keeps the index in sync with DML"),
        "CREATE TABLE words50d (\n"
        "  wordid  INT AUTO_INCREMENT PRIMARY KEY,\n"
        "  word    VARCHAR(200),\n"
        "  wordvec VARBINARY(208) COMMENT\n"
        "    'MYVECTOR COLUMN type=HNSW,dim=50,size=100000,M=32,ef=100,dist=L2,online=Y,idcol=wordid'\n"
        ");",
    ])
    exit_mysql(cast)

    # 5. load --------------------------------------------------------------
    chapter(cast, f"5. Load the {WORDS:,} most common English words")
    cast.comment("Each row is INSERT ... myvector_construct('[0.418 0.24968 ...]')", SHELL_PROMPT)
    shell(cast,
          f"(zcat examples/stanford50d/insert50d.sql.gz | head -n {WORDS + 1}; echo 'COMMIT;') \\\n"
          f"    | docker exec -i {CONTAINER} mysql -uroot -p{ROOT_PW} {DB} 2>/dev/null",
          actual=["bash", "-c",
                  f"(zcat examples/stanford50d/insert50d.sql.gz | head -n {WORDS + 1}; echo 'COMMIT;')"
                  f" | docker exec -i -e MYSQL_PWD={ROOT_PW} {CONTAINER} mysql -uroot {DB}"])

    # 6. build -------------------------------------------------------------
    chapter(cast, "6. Build the HNSW index")
    enter_mysql(cast)
    mysql_session(cast, [
        "SELECT COUNT(*) FROM words50d;",
        f"CALL mysql.myvector_index_build('{INDEX}', 'wordid');",
        ("--", "The binlog position is where online updates continue from"),
        ("vertical", f"CALL mysql.myvector_index_status('{INDEX}')"),

        # 7. online on ------------------------------------------------------
        ("chapter", "7. Turn on online updates"),
        ("--", "Online updates follow the binlog. On component images up to v1.26.9 the"),
        ("--", "listener only starts if the component is installed while MySQL accepts TCP."),
        ("--", "The image installs it during first-start init, so reinstall it once, and"),
        ("--", "load the saved index into the new instance:"),
        "UNINSTALL COMPONENT 'file://myvector';",
        "INSTALL COMPONENT 'file://myvector';",
        f"CALL mysql.myvector_index_load('{INDEX}');",
    ])

    # 8. search ------------------------------------------------------------
    chapter(cast, "8. Nearest-neighbour search (ANN)")
    mysql_session(cast, [
        "SET @q = (SELECT wordvec FROM words50d WHERE word = 'school');",
        ("--", "myvector_ann_set() returns the ids of the nearest rows, nearest first"),
        f"SELECT myvector_ann_set('{INDEX}', 'wordid', @q, 'nn=8') AS ids;",
        ("--", "JSON_TABLE turns the ids back into rows"),
        ann_query("@q", 8),
        "SET @q = (SELECT wordvec FROM words50d WHERE word = 'coffee');",
        ann_query("@q", 8),
        "SET @q = (SELECT wordvec FROM words50d WHERE word = 'guitar');",
        ann_query("@q", 8),
        # 9. exact ---------------------------------------------------------
        ("chapter", "9. Check it against an exact (brute-force) search"),
        ("--", "Same query, computed over every row: same answer, but it scans the table"),
        "SELECT word, ROUND(myvector_distance(wordvec, @q, 'L2'), 3) AS distance\n"
        "FROM words50d ORDER BY distance LIMIT 8;",
        ("--", "ef_search trades speed for recall, per query"),
        ann_query("@q", 8, "ef_search=400"),
    ])

    # 10. online -----------------------------------------------------------
    chapter(cast, "10. Online updates: new rows are searchable at once")
    mysql_session(cast, [
        "SET @db = (SELECT wordvec FROM words50d WHERE word = 'database');",
        ann_query("@db", 5),
        ("--", "Add a new word with the same meaning as 'database'"),
        "INSERT INTO words50d (word, wordvec) VALUES ('myvector', @db);",
        "DO SLEEP(2);  -- the binlog listener applies the change in the background",
        ann_query("@db", 5),
        ("--", "No rebuild. (On v1.26.9 only INSERTs are applied online; rebuild after"),
        ("--", "DELETE or UPDATE.)"),
    ])
    exit_mysql(cast)

    # 11. persistence ------------------------------------------------------
    chapter(cast, "11. The index is saved to disk")
    shell(cast, f"docker restart {CONTAINER}", actual=["docker", "restart", CONTAINER])
    wait_ready()
    enter_mysql(cast)
    mysql_session(cast, [
        f"CALL mysql.myvector_index_load('{INDEX}');",
        "SET @q = (SELECT wordvec FROM words50d WHERE word = 'computer');",
        ann_query("@q", 6),
        ("--", "Done with it? Drop the index (the table is untouched)"),
        f"CALL mysql.myvector_index_drop('{INDEX}');",
    ])
    exit_mysql(cast)

    chapter(cast, "That's MyVector")
    cast.output(
        "Docs:   https://myvector.online/\n"
        "Code:   https://github.com/askdba/myvector\n"
        "Images: https://github.com/askdba/myvector/pkgs/container/myvector",
        line_delay=0.3)
    cast.pause(4)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", default="ghcr.io/askdba/myvector:mysql9.7-component")
    ap.add_argument("--output", default=str(REPO / "docs/assets/demo/myvector-demo.cast"))
    ap.add_argument("--keep", action="store_true", help="leave the container running")
    args = ap.parse_args()

    cast = Cast()
    try:
        record(args.image, cast)
    finally:
        if not args.keep:
            run(["docker", "rm", "-f", CONTAINER], check=False)

    header = {"version": 2, "width": WIDTH, "height": HEIGHT,
              "title": f"MyVector demo ({args.image})",
              "env": {"TERM": "xterm-256color", "SHELL": "/bin/bash"}}
    out = pathlib.Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        f.write(json.dumps(header) + "\n")
        for ev in cast.events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    print(f"wrote {out} ({cast.t / 60:.1f} min, {len(cast.events)} events)")


if __name__ == "__main__":
    main()
