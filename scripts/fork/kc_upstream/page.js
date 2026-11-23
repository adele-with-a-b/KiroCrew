(function () {
  // Unticking a fix rewrites the reply to type in chat; the page itself sends nothing.
  var live = document.querySelector('[data-live-reply]');
  if (live) {
    var boxes = document.querySelectorAll('input[data-inc]');
    var code = live.querySelector('[data-reply]');
    var note = live.querySelector('[data-reply-note]');
    var base = live.getAttribute('data-base');
    var words = function (ns) {
      if (ns.length === 1) return ns[0];
      return ns.slice(0, -1).join(', ') + ' and ' + ns[ns.length - 1];
    };
    var update = function () {
      var on = [];
      boxes.forEach(function (b) {
        b.closest('.arow').classList.toggle('off', !b.checked);
        b.closest('.inc').querySelector('.inc-t').textContent = b.checked ? 'Send this one' : 'Kept back';
        if (b.checked) on.push(b.getAttribute('data-inc'));
      });
      if (on.length === boxes.length) {
        code.textContent = base;
        note.textContent = boxes.length === 1 ? 'It goes out.' : 'All ' + boxes.length + ' go out.';
      } else if (on.length === 0) {
        code.textContent = 'send none';
        note.textContent = 'They stay in your fork. I will not ask again.';
      } else {
        code.textContent = base + ', only ' + words(on);
        note.textContent = on.length === 1 ? 'Only fix ' + on[0] + ' goes out.' : 'Only fixes ' + words(on) + ' go out.';
      }
    };
    boxes.forEach(function (b) { b.addEventListener('change', update); });
    update();
  }
  document.querySelectorAll('.copy').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var text = btn.parentElement.querySelector('code').textContent;
      var done = function () {
        btn.textContent = 'Copied';
        btn.classList.add('did');
        setTimeout(function () { btn.textContent = 'Copy'; btn.classList.remove('did'); }, 1400);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, done);
      } else { done(); }
    });
  });
})();
