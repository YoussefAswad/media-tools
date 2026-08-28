// YAML syntax highlighting for the Config tab's raw editor. Only that page loads it.
(function () {
  var ta = document.getElementById("yaml-editor");
  var hl = document.getElementById("yaml-highlight");
  if (ta && hl) {
    var esc = function (s) {
      return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    };
    // color a single scalar value (already html-escaped) as bool / number / string
    var colorScalar = function (v) {
      var t = v.trim();
      if (t === "") return v;
      if (/^(true|false|null|~)$/i.test(t)) return '<span class="y-bool">' + v + "</span>";
      if (/^-?\d+(\.\d+)?$/.test(t)) return '<span class="y-num">' + v + "</span>";
      return '<span class="y-str">' + v + "</span>";
    };
    var highlight = function (src) {
      return src.split("\n").map(function (line) {
        var e = esc(line);
        if (/^\s*#/.test(line)) return '<span class="y-com">' + e + "</span>";
        // key:  value
        var m = e.match(/^(\s*(?:- )?)([A-Za-z0-9_.-]+)(:)(.*)$/);
        if (m) {
          var val = m[4].replace(/^(\s+)(.*)$/, function (_a, sp, rest) {
            if (rest === "") return sp;
            var r = rest;
            if (/^#/.test(rest)) return sp + '<span class="y-com">' + rest + "</span>";
            if (/^(true|false|null|~)\s*$/i.test(rest)) r = '<span class="y-bool">' + rest + "</span>";
            else if (/^-?\d+(\.\d+)?\s*$/.test(rest)) r = '<span class="y-num">' + rest + "</span>";
            else r = '<span class="y-str">' + rest + "</span>";
            return sp + r;
          });
          return m[1] + '<span class="y-key">' + m[2] + "</span>" + '<span class="y-punc">:</span>' + val;
        }
        // inline flow map:  - { key: val, key: val }
        var fm = e.match(/^(\s*(?:- )?)(\{)(.*)(\})(\s*)$/);
        if (fm) {
          var inner = fm[3].replace(
            /([A-Za-z0-9_.-]+)(\s*)(:)(\s*)([^,}]*)/g,
            function (_a, k, s1, colon, s2, val2) {
              return '<span class="y-key">' + k + "</span>" + s1 +
                '<span class="y-punc">' + colon + "</span>" + s2 + colorScalar(val2);
            }
          );
          return fm[1] + '<span class="y-punc">{</span>' + inner +
            '<span class="y-punc">}</span>' + fm[5];
        }
        return e;
      }).join("\n");
    };
    var sync = function () {
      hl.innerHTML = highlight(ta.value) + "\n";
      hl.scrollTop = ta.scrollTop;
      hl.scrollLeft = ta.scrollLeft;
    };
    ta.addEventListener("input", sync);
    ta.addEventListener("scroll", function () { hl.scrollTop = ta.scrollTop; hl.scrollLeft = ta.scrollLeft; });
    // keep tabs usable as two spaces
    ta.addEventListener("keydown", function (e) {
      if (e.key === "Tab") {
        e.preventDefault();
        var s = ta.selectionStart, en = ta.selectionEnd;
        ta.value = ta.value.slice(0, s) + "  " + ta.value.slice(en);
        ta.selectionStart = ta.selectionEnd = s + 2;
        sync();
      }
    });
    sync();
  }
})();
