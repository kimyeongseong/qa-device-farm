// Copy this file to stream-config.js and fill in your tunnel hostname.
//
// The dashboard (port 8001) and the stream server (ws-scrcpy, port 8000) are two
// separate processes. On a LAN both are reachable at the same host, so no config
// is needed. Once you expose the farm through a tunnel, the stream server gets
// its own public hostname and the browser has to be told what it is.
//
// stream-config.js is gitignored -- your hostname stays out of the repo.

window.STREAM_TUNNEL_HOST = 'your-stream-subdomain.loca.lt';
