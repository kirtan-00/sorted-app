/* One page view per load: path, referrer, browser. No cookies, no storage, nothing on Do Not Track. */
(function () {
  try {
    if (navigator.doNotTrack === '1') return;
    var K = 'sb_publishable_aAR90Pzf1gza49CP2t7uKQ_xyDhkTZI';
    var s = document.currentScript, root = '/', path = location.pathname;
    if (s && s.src) root = new URL('.', s.src).pathname;
    if (root !== '/' && path.indexOf(root) === 0) path = '/' + path.slice(root.length);
    fetch('https://woasffpwdbwavwcrtllz.supabase.co/rest/v1/rpc/site_hit', {
      method: 'POST', keepalive: true,
      headers: { 'content-type': 'application/json', 'apikey': K, 'authorization': 'Bearer ' + K },
      body: JSON.stringify({ p_path: path.slice(0, 200), p_ref: (document.referrer || '').slice(0, 300), p_ua: navigator.userAgent.slice(0, 300) })
    }).catch(function () {});
  } catch (e) {}
})();
