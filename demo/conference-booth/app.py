import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import mysql.connector

DB = dict(host=os.getenv('MYSQL_HOST','localhost'), port=int(os.getenv('MYSQL_PORT','3306')),
          user=os.getenv('MYSQL_USER','root'), password=os.getenv('MYSQL_PASSWORD','myvector'),
          database=os.getenv('MYSQL_DATABASE','vectordb'))

# Tiny deterministic query encoder for the booth. In a production version, replace this
# function with a SentenceTransformer embedding call, as in docs/DEMO.md.
def embed(q):
    q=q.lower()
    return [
      1.0 if any(x in q for x in ['rain','waterproof','wet']) else 0.0,
      1.0 if any(x in q for x in ['warm','cold','winter','hike','mountain']) else 0.0,
      1.0 if any(x in q for x in ['coffee','brew','outdoor','camp']) else 0.0,
      1.0 if any(x in q for x in ['travel','flight','charger','pack','commute']) else 0.0,
      1.0 if any(x in q for x in ['phone','laptop','usb','power','charge']) else 0.0,
    ]

def vec_literal(v):
    return '[' + ','.join(f'{x:.6f}' for x in v) + ']'

def search(q):
    v=vec_literal(embed(q))
    conn=mysql.connector.connect(**DB)
    cur=conn.cursor(dictionary=True)
    sql="""SELECT id,name,description,myvector_row_distance(id) AS distance
           FROM products
           WHERE MYVECTOR_IS_ANN('vectordb.products.vec','id',myvector_construct(%s),10)
           ORDER BY distance ASC"""
    cur.execute(sql,(v,))
    rows=cur.fetchall()
    cur.close(); conn.close()
    return rows

HTML='''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>MyVector Booth Demo</title><style>
body{font-family:system-ui;margin:0;background:#0b1020;color:#f7f8fb}.wrap{max-width:1050px;margin:auto;padding:48px 28px}.badge{color:#8ee6c0;font-weight:700}.hero{font-size:54px;line-height:1;margin:12px 0 18px}.sub{color:#aab3c5;font-size:20px;max-width:760px}.search{display:flex;gap:10px;margin:32px 0}.search input{flex:1;padding:20px;border-radius:12px;border:1px solid #39445d;background:#151c31;color:white;font-size:20px}.search button,.chip{padding:14px 18px;border:0;border-radius:10px;background:#8ee6c0;color:#071018;font-weight:800;cursor:pointer}.chips{display:flex;gap:8px;flex-wrap:wrap}.chip{background:#202a43;color:#dce4f2}.card{background:#121a2d;border:1px solid #293650;border-radius:16px;padding:20px;margin:12px 0}.score{float:right;color:#8ee6c0;font-weight:800}.meta{color:#8f9bb2}.reveal{margin-top:30px}.sql{background:#070b14;padding:18px;border-radius:12px;overflow:auto;color:#c9d5eb;display:none}</style></head><body><div class="wrap">
<div class="badge">MYSQL + MYVECTOR • CONFERENCE BOOTH</div><div class="hero">Search by meaning.</div><div class="sub">Ask for what you want in natural language. MyVector finds semantically relevant products using an HNSW vector index inside MySQL.</div>
<div class="search"><input id="q" placeholder="Try: something warm for a rainy hike"><button onclick="go()">Search</button></div><div class="chips"><button class="chip" onclick="demo('something warm for a rainy hike')">Rainy hike</button><button class="chip" onclick="demo('gift for someone who loves coffee')">Coffee gift</button><button class="chip" onclick="demo('lightweight travel charger')">Travel power</button></div><div id="results"></div>
<div class="reveal"><button class="chip" onclick="document.querySelector('.sql').style.display='block'">Show the SQL</button><pre class="sql">SELECT id, name, description, myvector_row_distance(id)
FROM products
WHERE MYVECTOR_IS_ANN(
  'vectordb.products.vec', 'id',
  myvector_construct('[query embedding]'), 10
)
ORDER BY myvector_row_distance(id);</pre></div></div><script>
async function go(){let q=document.getElementById('q').value;let r=await fetch('/search?q='+encodeURIComponent(q));let d=await r.json();document.getElementById('results').innerHTML='<h2>'+d.results.length+' relevant results</h2>'+d.results.map(x=>`<div class="card"><span class="score">distance ${Number(x.distance).toFixed(3)}</span><h2>${x.name}</h2><div>${x.description}</div><div class="meta">HNSW semantic match in MySQL</div></div>`).join('')}
function demo(x){document.getElementById('q').value=x;go()} go();</script></body></html>'''

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/search'):
            q=parse_qs(urlparse(self.path).query).get('q',[''])[0]
            try:
                data={'query':q,'results':search(q)}
            except Exception as e:
                data={'query':q,'results':[],'error':str(e)}
            body=json.dumps(data,default=str).encode()
            self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return
        body=HTML.encode(); self.send_response(200); self.send_header('Content-Type','text/html'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)

if __name__=='__main__':
    ThreadingHTTPServer(('0.0.0.0',int(os.getenv('PORT','8080'))),Handler).serve_forever()
