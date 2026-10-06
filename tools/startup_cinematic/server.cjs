const fs = require('fs');
const path = require('path');
const http = require('http');
const root = path.resolve(__dirname, '../..');
const types = {'.html':'text/html','.mjs':'text/javascript','.css':'text/css','.json':'application/json','.png':'image/png','.mp4':'video/mp4'};
exports.root = root;
exports.serve = async () => {
  const server = http.createServer((req, res) => {
    let filename;
    try { filename = path.resolve(root, '.' + decodeURIComponent(new URL(req.url, 'http://localhost').pathname)); }
    catch { res.writeHead(400).end(); return; }
    if (!filename.startsWith(root + path.sep) || !fs.existsSync(filename) || !fs.statSync(filename).isFile()) { res.writeHead(404).end(); return; }
    const size = fs.statSync(filename).size;
    const range = req.headers.range?.match(/^bytes=(\d+)-(\d*)$/);
    res.setHeader('Content-Type', types[path.extname(filename)] || 'application/octet-stream');
    res.setHeader('Accept-Ranges','bytes');
    if (range) {
      const start = Number(range[1]), end = Math.min(size - 1, range[2] ? Number(range[2]) : size - 1);
      if (start > end) { res.writeHead(416).end(); return; }
      res.writeHead(206, {'Content-Range':`bytes ${start}-${end}/${size}`,'Content-Length':end-start+1});
      fs.createReadStream(filename,{start,end}).pipe(res);
    } else { res.setHeader('Content-Length',size); fs.createReadStream(filename).pipe(res); }
  });
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  return {server,url:'http://127.0.0.1:' + server.address().port};
};
