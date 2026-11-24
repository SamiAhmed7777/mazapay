/* MazaPay Treasury — shared client JS (LANE 4).
 *
 * Exposes exactly two globals:
 *   window.trPost(url, body) -> Promise<json>   POST JSON + X-CSRF-Token
 *   window.trFmt(cents)      -> "$1,234.56"
 *
 * Everything else stays inside this closure. Every page must remain readable
 * without this file: nothing here builds content, it only enhances forms marked
 * with data-tr-json. No inline handlers, no external imports.
 */
'use strict';
(function () {
  'use strict';

  var CSRF_COOKIE = 'mazapay_tr_csrf';

  /* Read a cookie by name. Returns '' when absent (same-origin only). */
  function readCookie(name) {
    var jar = document.cookie ? document.cookie.split(';') : [];
    for (var i = 0; i < jar.length; i += 1) {
      var entry = jar[i].replace(/^\s+/, '');
      var cut = entry.indexOf('=');
      if (cut < 1) { continue; }
      if (entry.slice(0, cut) === name) {
        try { return decodeURIComponent(entry.slice(cut + 1)); } catch (e) { return ''; }
      }
    }
    return '';
  }

  /* POST JSON with the CSRF header; rejects with the server's error.message. */
  function trPost(url, body) {
    var headers = { 'Content-Type': 'application/json', 'Accept': 'application/json' };
    var token = readCookie(CSRF_COOKIE);
    if (token) { headers['X-CSRF-Token'] = token; }
    return fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: headers,
      body: JSON.stringify(body === undefined || body === null ? {} : body)
    }).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        if (text) {
          try { data = JSON.parse(text); } catch (e) { data = null; }
        }
        if (!res.ok) {
          var payload = data && data.error ? data.error : null;
          var message = (payload && payload.message) || ('Request failed (' + res.status + ')');
          var err = new Error(message);
          err.status = res.status;
          err.code = (payload && payload.code) || '';
          throw err;
        }
        return data === null ? {} : data;
      });
    });
  }

  /* Integer cents -> "$1,234.56". Non-finite input renders an em dash. */
  function trFmt(cents) {
    var value = Number(cents);
    if (!isFinite(value)) { return '\u2014'; }
    var negative = value < 0;
    var total = Math.round(Math.abs(value));
    var dollars = String(Math.floor(total / 100));
    var rest = total % 100;
    var grouped = dollars.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    var centsText = rest < 10 ? '0' + rest : String(rest);
    return (negative ? '-' : '') + '$' + grouped + '.' + centsText;
  }

  window.trPost = trPost;
  window.trFmt = trFmt;

  /* ---- progressive enhancement: forms that post JSON ---------------------- */

  function fieldValue(form, name) {
    var el = form.elements.namedItem(name);
    if (!el) { return undefined; }
    if (el.length && !el.tagName) { el = el[0]; }  /* RadioNodeList */
    var raw = el.value === undefined ? '' : String(el.value).trim();
    return raw === '' ? undefined : raw;
  }

  function setStatus(form, text, isError) {
    var node = form.querySelector('[data-tr-status]');
    if (node) {
      node.textContent = text;
      node.setAttribute('data-tr-state', isError ? 'error' : 'ok');
    }
  }

  function wireForm(form) {
    var endpoint = form.getAttribute('data-tr-json');
    if (!endpoint) { return; }
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      var button = form.querySelector('button[type="submit"], button:not([type])');
      var payload = {};
      var i;
      for (i = 0; i < form.elements.length; i += 1) {
        var el = form.elements[i];
        if (!el.name || el.disabled) { continue; }
        if (el.type === 'submit' || el.type === 'button' || el.type === 'reset') { continue; }
        var value = fieldValue(form, el.name);
        if (value !== undefined) { payload[el.name] = value; }
      }
      if (button) { button.disabled = true; }
      setStatus(form, 'Recording…', false);
      trPost(endpoint, payload).then(function () {
        setStatus(form, 'Recorded — refreshing the pool…', false);
        window.setTimeout(function () { document.location.reload(); }, 500);
      })['catch'](function (err) {
        setStatus(form, err && err.message ? err.message : 'Could not record that.', true);
        if (button) { button.disabled = false; }
      });
    });
  }

  function boot() {
    if (document.documentElement.getAttribute('data-tr-js') === '1') { return; }
    document.documentElement.setAttribute('data-tr-js', '1');
    var forms = document.querySelectorAll('form[data-tr-json]');
    for (var i = 0; i < forms.length; i += 1) { wireForm(forms[i]); }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
