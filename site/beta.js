/* sorted beta sign-up. Renders into #beta on the landing page.
   Sign-in is a six digit code sent to the address (Supabase Auth email OTP), so an address
   that cannot receive mail never gets an account. The 48 hour window starts when the code is
   verified (claim_access on the server), and the access page at beta/ shows the clock. */
(function () {
  var SUPABASE_URL = 'https://woasffpwdbwavwcrtllz.supabase.co';
  var SUPABASE_KEY = 'sb_publishable_aAR90Pzf1gza49CP2t7uKQ_xyDhkTZI';
  var root = document.getElementById('beta');
  if (!root || !window.supabase) return;
  var sb = window.supabase.createClient(SUPABASE_URL, SUPABASE_KEY);

  root.innerHTML =
    '<div class="block sky wrap beta-block">' +
      '<div class="beta-grid">' +
        '<div class="beta-copy">' +
          '<p class="badge">public beta, testing right now</p>' +
          '<h2>Join the beta.</h2>' +
          '<p>A name, an email, what you shoot. A six digit code comes back to that address; enter it and your 48 hours of early access start on the spot. The build and what to try first are on the access page.</p>' +
          '<p class="beta-fine">Your details stay with us. No list is sold, no newsletter, one email when the build changes.</p>' +
        '</div>' +
        '<form class="beta-form" id="beta-form" novalidate>' +
          '<label><span>Name</span><input name="name" type="text" autocomplete="name" required maxlength="120"></label>' +
          '<label><span>Email</span><input name="email" type="email" autocomplete="email" inputmode="email" required maxlength="200"></label>' +
          '<label><span>Studio or company</span><input name="studio" type="text" autocomplete="organization" maxlength="120" placeholder="optional"></label>' +
          '<div class="beta-two">' +
            '<label><span>What you shoot or cut</span><select name="work" required>' +
              '<option value="">choose</option><option>weddings</option><option>documentary</option><option>agency, commercial</option>' +
              '<option>editor, post house</option><option>photographer</option><option>designer</option><option>other</option></select></label>' +
            '<label><span>A typical shoot</span><select name="data_size" required>' +
              '<option value="">choose</option><option>under 100 GB</option><option>100 to 500 GB</option><option>500 GB to 2 TB</option><option>more than 2 TB</option></select></label>' +
          '</div>' +
          '<label><span>Your Mac</span><select name="mac" required>' +
            '<option value="">choose</option><option>Apple silicon (M1 to M4)</option><option>Intel</option><option>not sure</option></select></label>' +
          '<label><span>Anything we should know</span><textarea name="note" rows="2" maxlength="1000" placeholder="optional"></textarea></label>' +
          '<button type="submit" class="cta beta-submit">Send me the code</button>' +
          '<p class="beta-msg" id="beta-msg" aria-live="polite"></p>' +
        '</form>' +
        '<form class="beta-form beta-code" id="beta-code" hidden novalidate>' +
          '<p class="beta-sent">Code sent to <b id="beta-to"></b>. Check spam if it takes more than a minute.</p>' +
          '<label><span>Six digit code</span><input name="token" type="text" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required></label>' +
          '<button type="submit" class="cta beta-submit">Start my 48 hours</button>' +
          '<button type="button" class="beta-link" id="beta-again">Send a new code</button>' +
          '<p class="beta-msg" id="beta-msg2" aria-live="polite"></p>' +
        '</form>' +
      '</div>' +
    '</div>';

  var form = document.getElementById('beta-form');
  var codeForm = document.getElementById('beta-code');
  var msg = document.getElementById('beta-msg');
  var msg2 = document.getElementById('beta-msg2');
  var pending = null;

  function say(el, text, bad) { el.textContent = text; el.classList.toggle('bad', !!bad); }
  function busy(f, on) { f.querySelector('.beta-submit').disabled = on; f.classList.toggle('busy', on); }

  function validEmail(s) { return /^[^@\s]+@[^@\s]+\.[^@\s]{2,}$/.test(s); }

  function sendCode(data) {
    return sb.auth.signInWithOtp({
      email: data.email,
      options: { shouldCreateUser: true, data: { name: data.name, studio: data.studio, work: data.work, data_size: data.data_size, mac: data.mac, note: data.note } }
    });
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var d = {};
    new FormData(form).forEach(function (v, k) { d[k] = String(v).trim(); });
    d.email = d.email.toLowerCase();
    if (!d.name) return say(msg, 'Your name, please.', true);
    if (!validEmail(d.email)) return say(msg, 'That email does not look right.', true);
    if (!d.work || !d.data_size || !d.mac) return say(msg, 'Three quick picks left: what you shoot, shoot size, your Mac.', true);
    busy(form, true); say(msg, 'Sending the code');
    sendCode(d).then(function (r) {
      busy(form, false);
      if (r.error) return say(msg, r.error.message.indexOf('rate') > -1 ? 'Too many codes for now. Try again in a few minutes.' : 'Could not send the code: ' + r.error.message, true);
      pending = d;
      document.getElementById('beta-to').textContent = d.email;
      form.hidden = true; codeForm.hidden = false;
      codeForm.querySelector('input[name=token]').focus();
    });
  });

  codeForm.addEventListener('submit', function (e) {
    e.preventDefault();
    var token = codeForm.querySelector('input[name=token]').value.replace(/\D/g, '');
    if (token.length !== 6) return say(msg2, 'Six digits.', true);
    busy(codeForm, true); say(msg2, 'Checking');
    sb.auth.verifyOtp({ email: pending.email, token: token, type: 'email' }).then(function (r) {
      if (r.error) { busy(codeForm, false); return say(msg2, 'That code did not match. Codes last ten minutes.', true); }
      return sb.rpc('claim_access').then(function () { location.href = 'beta/'; });
    });
  });

  document.getElementById('beta-again').addEventListener('click', function () {
    if (!pending) return;
    say(msg2, 'Sending a new code');
    sendCode(pending).then(function (r) { say(msg2, r.error ? 'Could not resend: ' + r.error.message : 'New code sent.', !!r.error); });
  });
})();
