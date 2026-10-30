/* Loft site: theme choice, mobile menu, copy buttons, and the language on /pair. No tracking, no cookies, no network.
   /pair carries the pairing details in its URL fragment: nothing here reads location.hash, and it must stay that way. */
(function () {
  var KEY = "loft-theme";              // "light" | "dark"; absent = follow the system
  var root = document.documentElement;
  root.classList.add("js");

  function stored() {
    try { var v = localStorage.getItem(KEY); return v === "light" || v === "dark" ? v : "system"; }
    catch (e) { return "system"; }
  }

  // <picture> media queries can't see data-theme, so dark <source>s are rewritten to match the choice.
  // Each dark source keeps its width condition in data-mq.
  function syncPictures(choice) {
    var sources = document.querySelectorAll('source[data-scheme="dark"]');
    for (var i = 0; i < sources.length; i++) {
      var mq = sources[i].getAttribute("data-mq");
      var media;
      if (choice === "dark") media = mq || "all";
      else if (choice === "light") media = "not all";
      else media = "(prefers-color-scheme: dark)" + (mq ? " and " + mq : "");
      sources[i].media = media;
    }
  }

  function apply(choice) {
    if (choice === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", choice);
    root.setAttribute("data-theme-choice", choice);
    var btn = document.querySelector(".theme-toggle");
    if (btn) {
      var label = btn.getAttribute("data-label-" + choice);
      btn.setAttribute("aria-label", label);
      btn.setAttribute("title", label);
    }
    syncPictures(choice);
  }

  // Before first paint: set data-theme so the colours don't flash.
  var choice = stored();
  if (choice !== "system") root.setAttribute("data-theme", choice);
  root.setAttribute("data-theme-choice", choice);

  // /pair has no language prefix, so it carries both languages ([data-lang]) and shows the one the browser
  // asks for: Norwegian for nb, nn and no, English otherwise. Without JavaScript both show, English first.
  function setLang(l) {
    root.setAttribute("data-lang-choice", l);
    root.lang = l === "no" ? "nb" : "en";
    var t = document.body && document.body.getAttribute("data-title-" + l);
    if (t) document.title = t;
  }
  var bilingual = root.hasAttribute("data-bilingual");
  if (bilingual) setLang(/^(nb|nn|no)(-|_|$)/i.test(navigator.language || "") ? "no" : "en");

  document.addEventListener("DOMContentLoaded", function () {
    apply(choice);

    if (bilingual) {
      setLang(root.getAttribute("data-lang-choice"));
      var toggles = document.querySelectorAll(".lang-toggle");
      for (var t = 0; t < toggles.length; t++) toggles[t].addEventListener("click", function (e) {
        var to = e.currentTarget.getAttribute("data-to");
        setLang(to);
        var next = document.querySelector('.lang-toggle[data-lang="' + to + '"]');
        if (next) next.focus();
      });
    }

    var btn = document.querySelector(".theme-toggle");
    if (btn) btn.addEventListener("click", function () {
      choice = { system: "light", light: "dark", dark: "system" }[choice];
      try { if (choice === "system") localStorage.removeItem(KEY); else localStorage.setItem(KEY, choice); }
      catch (e) { /* private mode: the choice lasts for this page only */ }
      apply(choice);
    });

    var menu = document.querySelector(".menu-toggle");
    if (menu) menu.addEventListener("click", function () {
      var open = menu.getAttribute("aria-expanded") !== "true";
      menu.setAttribute("aria-expanded", String(open));
      root.classList.toggle("menu-open", open);
    });

    var blocks = document.querySelectorAll("pre[data-copy]");
    for (var i = 0; i < blocks.length; i++) {
      (function (pre) {
        var b = document.createElement("button");
        b.type = "button";
        b.className = "copy";
        b.textContent = pre.getAttribute("data-copy");
        b.addEventListener("click", function () {
          var text = pre.querySelector("code").textContent;
          navigator.clipboard.writeText(text).then(function () {
            var was = b.textContent;
            b.textContent = pre.getAttribute("data-copied");
            setTimeout(function () { b.textContent = was; }, 1600);
          });
        });
        pre.appendChild(b);
      })(blocks[i]);
    }
  });
})();
