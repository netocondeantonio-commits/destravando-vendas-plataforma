/*!
 * Liquix - Widget de Afiliado "Destravando as Vendas"
 * Uso: <script src="https://pagamentos.liquix.tech/afiliados/widget.js" data-ref="SEUCODE"></script>
 * Injeta um card de divulgacao com o link de afiliado do influencer.
 * Estilos 100% isolados via Shadow DOM (nao vazam para o site nem sofrem do site).
 */
(function () {
  "use strict";

  var COURSE_URL = "https://curso.liquix.tech/";
  var FALLBACK_REF = "site"; // usado quando data-ref vem vazio
  var MOUNT_ID = "liquix-curso"; // div opcional onde o card deve ser inserido

  // 1) Localiza a propria tag <script> (com fallback p/ navegadores sem currentScript)
  function findTag() {
    if (document.currentScript) return document.currentScript;
    var all = document.getElementsByTagName("script");
    for (var i = all.length - 1; i >= 0; i--) {
      var src = all[i].getAttribute("src") || "";
      if (src.indexOf("widget.js") !== -1 && src.indexOf("afiliados") !== -1) return all[i];
    }
    return null;
  }

  var tag = findTag();

  // 2) Le o codigo de afiliado (data-ref) com fallback para ref vazio
  var ref = tag ? (tag.getAttribute("data-ref") || "").trim() : "";
  if (!ref) ref = FALLBACK_REF;
  var url = COURSE_URL + "?ref=" + encodeURIComponent(ref);

  // 3) Cria o host e isola tudo no Shadow DOM
  var host = document.createElement("div");
  host.className = "liquix-curso-widget";
  host.style.cssText = "display:block;max-width:360px;margin:16px 0;";
  if (!host.attachShadow) return; // sem Shadow DOM nao renderiza, para nunca vazar estilo
  var root = host.attachShadow({ mode: "open" });

  var css =
    "*{box-sizing:border-box;margin:0;padding:0}" +
    ":host{display:block}" +
    ".c{position:relative;overflow:hidden;width:100%;max-width:360px;padding:26px 24px 20px;" +
      "border-radius:20px;border:1px solid rgba(255,255,255,.08);color:#fff;text-align:left;" +
      "line-height:1.4;letter-spacing:normal;" +
      "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;" +
      "background:linear-gradient(145deg,#06110f 0%,#0c524a 70%,#0e8c7f 115%);" +
      "box-shadow:0 18px 42px -18px rgba(0,0,0,.65)}" +
    ".c::before{content:'';position:absolute;top:-45px;right:-45px;width:170px;height:170px;" +
      "background:radial-gradient(circle,rgba(14,140,127,.45),transparent 70%);pointer-events:none}" +
    ".b{position:relative;width:52px;height:52px;border-radius:15px;display:flex;align-items:center;" +
      "justify-content:center;font-size:27px;font-weight:800;color:#06110f;margin-bottom:18px;" +
      "background:linear-gradient(135deg,#19d9bb,#0e8c7f);box-shadow:0 8px 18px -6px rgba(14,140,127,.7)}" +
    ".t{position:relative;font-size:21px;font-weight:700;letter-spacing:-.2px;margin-bottom:8px}" +
    ".s{position:relative;font-size:14px;font-weight:400;color:rgba(255,255,255,.72);margin-bottom:22px}" +
    ".a{position:relative;display:block;width:100%;padding:14px 20px;border-radius:12px;color:#06110f;" +
      "font-size:15px;font-weight:700;text-align:center;text-decoration:none;cursor:pointer;" +
      "background:linear-gradient(135deg,#1fe0c0,#0e8c7f);box-shadow:0 10px 24px -10px rgba(31,224,192,.6);" +
      "transition:transform .15s ease,filter .15s ease,box-shadow .15s ease}" +
    ".a:hover{transform:translateY(-2px);filter:brightness(1.05);box-shadow:0 14px 30px -10px rgba(31,224,192,.75)}" +
    ".a:active{transform:translateY(0)}" +
    ".f{position:relative;margin-top:12px;font-size:11px;text-align:center;color:rgba(255,255,255,.4)}";

  root.innerHTML =
    "<style>" + css + "</style>" +
    "<div class=\"c\">" +
      "<div class=\"b\">D</div>" +
      "<div class=\"t\">Destravando as Vendas</div>" +
      "<div class=\"s\">Venda em 5 marketplaces, do zero ao avan\u00e7ado</div>" +
      "<a class=\"a\" target=\"_blank\" rel=\"noopener noreferrer\">Quero come\u00e7ar</a>" +
      "<div class=\"f\">curso.liquix.tech</div>" +
    "</div>";

  var link = root.querySelector(".a");
  link.setAttribute("href", url);
  link.setAttribute("aria-label", "Quero come\u00e7ar - Destravando as Vendas");

  // 4) Insere no #liquix-curso se existir; senao, logo apos a tag <script>
  var target = document.getElementById(MOUNT_ID);
  if (target) {
    target.appendChild(host);
  } else if (tag && tag.parentNode) {
    tag.parentNode.insertBefore(host, tag.nextSibling);
  } else {
    (document.body || document.documentElement).appendChild(host);
  }
})();