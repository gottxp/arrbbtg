/*
  Painel do scanner de arbitragem (especificacao, secao 7). JavaScript puro, sem nada para compilar.

  O que esta pagina faz:
    - Tabela ao vivo: recebe os sinais pelo WebSocket /ws/sinais e redesenha no maximo a cada 200 ms.
    - Filtros: cada mudanca vai para o scanner (PUT /api/sessao) e, se houver um grupo escolhido,
      tambem e salva nele (POST /api/filtros/{nome}).
    - Grafico do historico de um sinal (GET /api/historico/{chave}), com a biblioteca Lightweight Charts.
    - Barra de status: ultima atualizacao, quantidade, saude das corretoras, pausar e busca.

  Regras seguidas no codigo todo:
    - Dado que vem do servidor entra na pagina SEMPRE por textContent / createElement (nunca como HTML):
      nomes de simbolos vem das corretoras e nao sao confiaveis.
    - Horarios sempre no fuso de Brasilia; numeros no formato brasileiro (virgula decimal).

  Secoes (procure pelos titulos "=====" para navegar):
    1. Constantes            6. Grupos de filtros     11. Grafico do historico
    2. Utilidades da pagina  7. WebSocket             12. Inicio
    3. Formatacao            8. Tabela
    4. Estado e servidor     9. Ordenacao e busca
    5. Formulario de filtros 10. Barra de status
*/
"use strict";

// ============================================================================
// 1. Constantes
// ============================================================================

const INTERVALO_DESENHO_MS = 200;      // no maximo um redesenho da tabela a cada 200 ms
const ESPERA_DIGITACAO_MS = 300;       // espera o dono parar de digitar antes de mandar o filtro
const INTERVALO_STATUS_MS = 3000;      // de quanto em quanto tempo pergunta a saude das corretoras
const RECONEXAO_INICIAL_MS = 1000;     // primeira espera para reconectar o WebSocket...
const RECONEXAO_MAXIMA_MS = 30000;     // ...que dobra a cada falha, ate 30 s
const ESPERA_BIBLIOTECA_MS = 15000;    // quanto esperar a biblioteca do grafico chegar
const FUSO = "America/Sao_Paulo";      // horario de Brasilia

// Nomes guardados no navegador (localStorage). So conveniencia: sem eles o painel funciona igual.
const GUARDADO_GRUPO = "scanner.grupo";
const GUARDADO_FILTROS_RECOLHIDOS = "scanner.filtrosRecolhidos";

// Nome bonito de cada corretora (a que nao estiver aqui aparece com o nome do config.yaml).
const NOMES_CORRETORAS = {
  mexc: "MEXC", gate: "Gate", bitget: "Bitget", bingx: "BingX", xt: "XT", hyperliquid: "Hyperliquid",
  kraken: "Kraken Pro", krakenfutures: "Kraken Pro", lighter: "Lighter", lighter_robinhood: "Lighter Robinhood",
  deepcoin: "Deepcoin",
};
const NOMES_MERCADOS = { spot: "spot", futuro: "futuros" };
const NOMES_TIPOS = { spot_futuro: "SPOT — FUTUROS", futuro_futuro: "FUTUROS — FUTUROS" };

// Opcoes das listas com caixinhas (o atributo data-opcoes do index.html escolhe qual).
const OPCOES_FIXAS = {
  tipos: [
    { valor: "spot_futuro", rotulo: "Spot — Futuros" },
    { valor: "futuro_futuro", rotulo: "Futuros — Futuros" },
  ],
  intervalos: [
    { valor: 1, rotulo: "1h" },
    { valor: 4, rotulo: "4h" },
    { valor: 8, rotulo: "8h" },
  ],
};

// Estados de saude que o /api/status informa, com o texto mostrado no rodape.
const TEXTOS_SAUDE = { conectada: "conectada", reconectando: "reconectando", atrasada: "atrasada" };

// Desenhos dos icones (caminhos SVG simples, feitos para este painel).
const ICONES = {
  fixar: "M9 3h6l-1 6 3 4H7l3-4-1-6zM12 13v8",
  grafico: "M4 4v16h16M7 15l4-4 3 3 6-7",
};

// ============================================================================
// 2. Utilidades da pagina
// ============================================================================

/** O elemento com este id. */
function porId(id) {
  return document.getElementById(id);
}

/** Cria um elemento com classe e texto (o texto entra como texto puro, nunca como HTML). */
function criar(tag, classe, texto) {
  const elemento = document.createElement(tag);
  if (classe) elemento.className = classe;
  if (texto !== undefined && texto !== null) elemento.textContent = String(texto);
  return elemento;
}

/** Um icone SVG (dos desenhos em ICONES). */
function icone(nome) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", "icone");
  const caminho = document.createElementNS(ns, "path");
  caminho.setAttribute("d", ICONES[nome]);
  svg.appendChild(caminho);
  return svg;
}

/** Le algo guardado no navegador; sem localStorage (modo privado, bloqueado) devolve null. */
function lerGuardado(nome) {
  try {
    return window.localStorage.getItem(nome);
  } catch (erro) {
    return null;
  }
}

/** Guarda algo no navegador (valor null apaga); sem localStorage, nao faz nada. */
function guardar(nome, valor) {
  try {
    if (valor === null || valor === undefined || valor === "") window.localStorage.removeItem(nome);
    else window.localStorage.setItem(nome, valor);
  } catch (erro) {
    // sem localStorage o painel funciona igual; so nao lembra a escolha
  }
}

/** Espera `ms` milissegundos. */
function esperar(ms) {
  return new Promise((resolver) => setTimeout(resolver, ms));
}

/** O texto de um objeto com as chaves em ordem (para comparar dois filtros). */
function textoEstavel(valor) {
  if (Array.isArray(valor)) return "[" + valor.map(textoEstavel).join(",") + "]";
  if (valor && typeof valor === "object") {
    return "{" + Object.keys(valor).sort().map((k) => JSON.stringify(k) + ":" + textoEstavel(valor[k])).join(",") + "}";
  }
  return JSON.stringify(valor === undefined ? null : valor);
}

// ============================================================================
// 3. Formatacao (numeros pt-BR e horarios de Brasilia)
// ============================================================================

/** True para um numero de verdade (nao null, nao NaN, nao infinito). */
function numeroValido(valor) {
  return typeof valor === "number" && Number.isFinite(valor);
}

const formatoPct = new Intl.NumberFormat("pt-BR", {
  minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: "exceptZero",
});
const formatoPctSemSinal = new Intl.NumberFormat("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const formatoUmaCasa = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 1 });
const formatoDuasCasas = new Intl.NumberFormat("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const formatoInteiro = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 0 });
const formatoPrecoGrande = new Intl.NumberFormat("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const formatoPrecoMedio = new Intl.NumberFormat("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 4 });
const formatoPrecoPequeno = new Intl.NumberFormat("pt-BR", { maximumSignificantDigits: 4 });

/** Percentual com sinal: 0.4 -> "+0,40%". Sem valor: "—". */
function pct(valor) {
  return numeroValido(valor) ? formatoPct.format(valor) + "%" : "—";
}

/** Classe de cor: verde para positivo, vermelho para negativo. */
function classeSinal(valor) {
  if (!numeroValido(valor) || valor === 0) return "neutro";
  return valor > 0 ? "positivo" : "negativo";
}

/** Dolares compactos: 56300 -> "$56,3K"; 1200000 -> "$1,2M"; 84.88 -> "$84,88". */
function usdCompacto(valor) {
  if (!numeroValido(valor)) return "—";
  const sinal = valor < 0 ? "-" : "";
  const absoluto = Math.abs(valor);
  const escalas = [[1e9, "B"], [1e6, "M"], [1e3, "K"]];
  for (const [tamanho, letra] of escalas) {
    // 999.960 arredonda para "1.000K": nesse caso ja passa para a escala de cima.
    if (absoluto >= tamanho * 0.99995) return `${sinal}$${formatoUmaCasa.format(absoluto / tamanho)}${letra}`;
  }
  return `${sinal}$${formatoDuasCasas.format(absoluto)}`;
}

/** Dolares com 2 casas e sinal so no negativo: 0.45 -> "$0,45"; -1.2 -> "-$1,20". */
function usd(valor) {
  if (!numeroValido(valor)) return "—";
  return (valor < 0 ? "-$" : "$") + formatoDuasCasas.format(Math.abs(valor));
}

/** Preco: casas decimais conforme o tamanho (89,14 · 2,6780 · 0,00001234). */
function preco(valor) {
  if (!numeroValido(valor)) return "—";
  const absoluto = Math.abs(valor);
  if (absoluto >= 1000) return formatoPrecoGrande.format(valor);
  if (absoluto >= 1) return formatoPrecoMedio.format(valor);
  return formatoPrecoPequeno.format(valor);
}

const formatoHora = new Intl.DateTimeFormat("pt-BR", {
  timeZone: FUSO, hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
});
const formatoHoraMinuto = new Intl.DateTimeFormat("pt-BR", {
  timeZone: FUSO, hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});
const formatoDiaMes = new Intl.DateTimeFormat("pt-BR", { timeZone: FUSO, day: "2-digit", month: "2-digit" });
const formatoMes = new Intl.DateTimeFormat("pt-BR", { timeZone: FUSO, month: "short" });
const formatoAno = new Intl.DateTimeFormat("pt-BR", { timeZone: FUSO, year: "numeric" });

/** "15:04:12" em Brasilia (ms = milissegundos desde 1970). Com `comMs`: "15:04:12.345". */
function horaBrasilia(ms, comMs) {
  if (!numeroValido(ms)) return "—";
  const data = new Date(ms);
  const texto = formatoHora.format(data);
  return comMs ? texto + "." + String(data.getUTCMilliseconds()).padStart(3, "0") : texto;
}

/** "15:04" em Brasilia. */
function horaMinutoBrasilia(ms) {
  return numeroValido(ms) ? formatoHoraMinuto.format(new Date(ms)) : "—";
}

/** "20/10 15:04:12" em Brasilia. */
function diaHoraBrasilia(ms) {
  return numeroValido(ms) ? formatoDiaMes.format(new Date(ms)) + " " + horaBrasilia(ms) : "—";
}

/** Nome bonito da corretora ("bitget" -> "Bitget"). */
function nomeCorretora(id) {
  const texto = String(id || "");
  return NOMES_CORRETORAS[texto.toLowerCase()] || texto;
}

/** Rotulo de uma opcao "corretora:mercado" ("gate:spot" -> "Gate spot"; "gate" -> "Gate (todos)"). */
function rotuloCorretoraMercado(valor) {
  const [corretora, mercado] = String(valor).split(":");
  if (!mercado) return `${nomeCorretora(corretora)} (todos)`;
  return `${nomeCorretora(corretora)} ${NOMES_MERCADOS[mercado] || mercado}`;
}

/** Numero em texto para o formulario, com virgula: 0.5 -> "0,5". */
function numeroParaCampo(valor) {
  return numeroValido(valor) ? String(valor).replace(".", ",") : "";
}

/**
 * Le um numero digitado ("0,5", "0.5", "-0,1", "1.000,5"). Devolve null se o texto nao for um numero.
 * Virgula ou ponto servem de separador decimal; com os dois, o ponto e separador de milhar.
 */
function lerNumeroDigitado(texto) {
  let limpo = texto.replace(/\s/g, "");
  if (limpo.includes(",") && limpo.includes(".")) limpo = limpo.replace(/\./g, "");
  limpo = limpo.replace(",", ".");
  if (!/^[-+]?(\d+\.?\d*|\.\d+)(e[-+]?\d+)?$/i.test(limpo)) return null;
  const numero = Number(limpo);
  return Number.isFinite(numero) ? numero : null;
}

// ============================================================================
// 4. Estado e conversa com o servidor
// ============================================================================

const estado = {
  sinais: new Map(),      // chave do sinal -> sinal (o JSON da especificacao, secao 5)
  congelados: null,       // com o painel pausado: a foto da tabela no momento da pausa
  sessao: null,           // a sessao que o servidor confirmou: {modo, filtros, comparacao}
  modo: "lucro",          // o que esta escolhido na tela
  comparacao: false,
  fixados: [],            // chaves dos sinais fixados no topo (campo `fixados` dos filtros)
  grupos: {},             // grupos salvos: {nome: filtros}
  grupoSelecionado: "",   // "" = nenhum grupo
  busca: "",              // texto da busca por moeda (filtra so na tela)
  conexao: "conectando",  // conectando | ao_vivo | reconectando
  proximaTentativaMs: null,
  ultimaMensagemMs: null, // quando chegou a ultima mensagem do WebSocket
  corretoras: [],         // nomes das corretoras (do /api/status)
  saude: null,            // ultima resposta do /api/status
  saudeFalhou: false,
  visiveis: 0,
  total: 0,
};

/** Erro de um pedido ao servidor, com a mensagem pronta para mostrar. */
class ErroServidor extends Error {
  constructor(mensagem, status) {
    super(mensagem);
    this.status = status;
  }
}

/** A mensagem de erro que o servidor mandou (o `detail` do FastAPI: texto, ou lista no erro 422). */
function textoDoErro(dados, status) {
  const detalhe = dados && dados.detail;
  if (typeof detalhe === "string") return detalhe;
  if (Array.isArray(detalhe)) {
    return detalhe.map((item) => {
      const lugar = Array.isArray(item && item.loc) ? item.loc.filter((parte) => parte !== "body").join(".") : "";
      const texto = item && item.msg ? String(item.msg) : "valor inválido";
      return lugar ? `${lugar}: ${texto}` : texto;
    }).join("\n");
  }
  return `o servidor respondeu com erro ${status}`;
}

/** Faz um pedido HTTP ao scanner e devolve o JSON da resposta. Em erro, levanta ErroServidor. */
async function pedir(metodo, caminho, corpo) {
  const opcoes = { method: metodo, headers: {}, cache: "no-store" };
  if (corpo !== undefined) {
    opcoes.headers["Content-Type"] = "application/json";
    opcoes.body = JSON.stringify(corpo);
  }
  let resposta;
  try {
    resposta = await fetch(caminho, opcoes);
  } catch (erro) {
    throw new ErroServidor("sem conexão com o scanner", 0);
  }
  let dados = null;
  try {
    dados = await resposta.json();
  } catch (erro) {
    dados = null;
  }
  if (!resposta.ok) throw new ErroServidor(textoDoErro(dados, resposta.status), resposta.status);
  return dados;
}

/** A chave de um sinal: par|tipo|corretora_long|mercado_long|corretora_short|mercado_short (igual a do motor). */
function chaveDoSinal(sinal) {
  const long = sinal.ponta_long || {};
  const short = sinal.ponta_short || {};
  return [sinal.par, sinal.tipo, long.corretora, long.mercado, short.corretora, short.mercado].join("|");
}

// ============================================================================
// 5. Formulario de filtros
// ============================================================================
//
// Cada elemento com data-campo vira um "controle" com duas funcoes:
//   ler()          -> o valor do campo para mandar ao servidor (vazio = null ou lista vazia)
//   escrever(v)    -> mostra o valor v vindo do servidor
// O tipo do controle vem do atributo data-tipo.

const controles = {};          // campo -> controle
const listasMulti = [];        // controles do tipo "multi" (para atualizar as opcoes de corretoras)

/** Erro de um campo digitado errado (o formulario mostra a mensagem e nao manda nada). */
class ErroCampo extends Error {}

/** O texto do rotulo de um campo (para as mensagens de erro). */
function rotuloDoCampo(elemento) {
  const campo = elemento.closest(".campo");
  const rotulo = campo && campo.querySelector("label, .rotulo");
  return rotulo ? rotulo.textContent.trim() : elemento.dataset.campo;
}

/** Campo de numero: texto vazio = null (filtro desligado). Aceita virgula ou ponto. */
function controleNumero(entrada) {
  entrada.addEventListener("input", () => {
    entrada.classList.toggle("invalido", entrada.value.trim() !== "" && lerNumeroDigitado(entrada.value) === null);
    aoMudarFiltro(ESPERA_DIGITACAO_MS);
  });
  return {
    ler() {
      const texto = entrada.value.trim();
      if (texto === "") return null;
      const numero = lerNumeroDigitado(texto);
      if (numero === null) throw new ErroCampo(`${rotuloDoCampo(entrada)}: "${texto}" não é um número`);
      return numero;
    },
    escrever(valor) {
      entrada.value = numeroParaCampo(valor);
      entrada.classList.remove("invalido");
    },
  };
}

/** Campo de lista de moedas: "BTC, ETH" <-> ["BTC", "ETH"]. */
function controleLista(entrada) {
  entrada.addEventListener("input", () => aoMudarFiltro(ESPERA_DIGITACAO_MS));
  return {
    ler() {
      const moedas = entrada.value.split(/[\s,;]+/).map((m) => m.trim().toUpperCase()).filter(Boolean);
      return [...new Set(moedas)];
    },
    escrever(valor) {
      entrada.value = Array.isArray(valor) ? valor.join(", ") : "";
    },
  };
}

/** Interruptor liga/desliga (true/false). */
function controleBooleano(caixa) {
  caixa.addEventListener("change", () => aoMudarFiltro(0));
  return {
    ler() {
      return caixa.checked;
    },
    escrever(valor) {
      caixa.checked = valor === true;
    },
  };
}

/** Caixa de selecao: opcao "" = null (usa o padrao). Valor desconhecido ganha uma opcao (nada se perde). */
function controleSelecao(caixa) {
  caixa.addEventListener("change", () => {
    aoMudarFiltro(0);
    desenharAgora(); // "ordenar por" muda a tabela na hora
  });
  return {
    ler() {
      return caixa.value === "" ? null : caixa.value;
    },
    escrever(valor) {
      const texto = valor === null || valor === undefined ? "" : String(valor);
      if (![...caixa.options].some((opcao) => opcao.value === texto)) caixa.appendChild(new Option(texto, texto));
      caixa.value = texto;
    },
  };
}

/** Lista com caixinhas (corretoras, tipos, intervalos de funding). Nenhuma marcada = filtro desligado. */
function controleMulti(caixa) {
  const botao = criar("button", "multi-botao");
  botao.type = "button";
  botao.setAttribute("aria-haspopup", "true");
  botao.setAttribute("aria-expanded", "false");
  const rotuloId = caixa.getAttribute("aria-labelledby");
  if (rotuloId) botao.setAttribute("aria-labelledby", rotuloId);
  const resumo = criar("span", "multi-resumo");
  botao.appendChild(resumo);
  const menu = criar("div", "multi-menu");
  menu.hidden = true;
  caixa.append(botao, menu);

  const textoVazio = caixa.dataset.vazio || "Todos";
  let marcados = [];  // valores marcados, no formato que o servidor guarda

  function opcoes() {
    const tipo = caixa.dataset.opcoes;
    let base;
    if (tipo === "corretoras") {
      base = [];
      for (const corretora of estado.corretoras) {
        for (const mercado of ["spot", "futuro"]) {
          const valor = `${corretora}:${mercado}`;
          base.push({ valor, rotulo: rotuloCorretoraMercado(valor) });
        }
      }
    } else {
      base = OPCOES_FIXAS[tipo] || [];
    }
    // Um valor salvo que nao esta nas opcoes (ex.: "gate" sem mercado) continua aparecendo.
    const conhecidos = new Set(base.map((opcao) => String(opcao.valor)));
    for (const valor of marcados) {
      if (!conhecidos.has(String(valor))) {
        base.push({ valor, rotulo: tipo === "corretoras" ? rotuloCorretoraMercado(valor) : String(valor) });
      }
    }
    return base;
  }

  function desenhar() {
    const lista = opcoes();
    const marcadosTexto = new Set(marcados.map(String));
    const rotulos = lista.filter((opcao) => marcadosTexto.has(String(opcao.valor))).map((opcao) => opcao.rotulo);
    resumo.textContent = rotulos.length ? rotulos.join(", ") : textoVazio;
    botao.title = resumo.textContent;
    menu.replaceChildren();
    for (const opcao of lista) {
      const linha = criar("label", "multi-opcao");
      const marcador = document.createElement("input");
      marcador.type = "checkbox";
      marcador.checked = marcadosTexto.has(String(opcao.valor));
      marcador.addEventListener("change", () => {
        const texto = String(opcao.valor);
        marcados = marcador.checked
          ? [...marcados.filter((v) => String(v) !== texto), opcao.valor]
          : marcados.filter((v) => String(v) !== texto);
        // A ordem salva segue a ordem das opcoes (fica igual nao importa a ordem dos cliques).
        const ordem = lista.map((o) => String(o.valor));
        marcados.sort((a, b) => ordem.indexOf(String(a)) - ordem.indexOf(String(b)));
        atualizarResumo();
        aoMudarFiltro(0);
      });
      linha.append(marcador, criar("span", null, opcao.rotulo));
      menu.appendChild(linha);
    }
    const limpar = criar("button", "multi-limpar", `Limpar (${textoVazio.toLowerCase()})`);
    limpar.type = "button";
    limpar.addEventListener("click", () => {
      marcados = [];
      desenhar();
      aoMudarFiltro(0);
    });
    menu.appendChild(limpar);
  }

  function atualizarResumo() {
    const marcadosTexto = new Set(marcados.map(String));
    const rotulos = opcoes().filter((o) => marcadosTexto.has(String(o.valor))).map((o) => o.rotulo);
    resumo.textContent = rotulos.length ? rotulos.join(", ") : textoVazio;
    botao.title = resumo.textContent;
  }

  function abrir(sim) {
    if (sim) {
      fecharMenus();
      desenhar();
    }
    menu.hidden = !sim;
    botao.setAttribute("aria-expanded", String(sim));
  }

  botao.addEventListener("click", (evento) => {
    evento.stopPropagation();
    abrir(menu.hidden);
  });
  menu.addEventListener("click", (evento) => evento.stopPropagation());

  const controle = {
    ler() {
      return marcados.slice();
    },
    escrever(valor) {
      marcados = Array.isArray(valor) ? valor.slice() : [];
      desenhar();
    },
    fechar() {
      abrir(false);
    },
    atualizarOpcoes() {
      if (menu.hidden) atualizarResumo();
      else desenhar();
    },
  };
  listasMulti.push(controle);
  desenhar();
  return controle;
}

/** Fecha todas as listas com caixinhas abertas. */
function fecharMenus() {
  for (const lista of listasMulti) lista.fechar();
}

/** Quantos sinais estao fixados, com o botao de desafixar todos. */
function controleFixados(caixa) {
  const texto = criar("span");
  const limpar = criar("button", "botao", "Desafixar todos");
  limpar.type = "button";
  limpar.addEventListener("click", () => {
    estado.fixados = [];
    atualizar();
    aoMudarFiltro(0);
    desenharAgora();
  });
  caixa.append(texto, limpar);

  function atualizar() {
    const quantidade = estado.fixados.length;
    texto.textContent = quantidade === 1 ? "1 sinal" : `${quantidade} sinais`;
    limpar.disabled = quantidade === 0;
  }

  const controle = {
    ler() {
      return estado.fixados.slice();
    },
    escrever(valor) {
      estado.fixados = Array.isArray(valor) ? valor.slice() : [];
      atualizar();
    },
    atualizar,
  };
  atualizar();
  return controle;
}

const CRIADORES_DE_CONTROLE = {
  numero: controleNumero,
  lista: controleLista,
  booleano: controleBooleano,
  selecao: controleSelecao,
  multi: controleMulti,
  fixados: controleFixados,
};

/** Liga cada elemento data-campo do index.html ao seu controle. */
function prepararFormulario() {
  for (const elemento of document.querySelectorAll("[data-campo]")) {
    const criador = CRIADORES_DE_CONTROLE[elemento.dataset.tipo];
    if (!criador) {
      console.warn("controle sem data-tipo conhecido:", elemento.dataset.campo);
      continue;
    }
    controles[elemento.dataset.campo] = criador(elemento);
  }
}

/**
 * Os filtros que estao no formulario. Devolve {filtros, erros}: `erros` tem as mensagens dos campos
 * digitados errado (com erro, nada e mandado ao servidor).
 * Comeca dos filtros que o servidor confirmou, para nunca perder um campo que o painel nao conhece.
 */
function lerFiltros() {
  const filtros = Object.assign({}, estado.sessao ? estado.sessao.filtros : {});
  const erros = [];
  for (const [campo, controle] of Object.entries(controles)) {
    try {
      filtros[campo] = controle.ler();
    } catch (erro) {
      if (!(erro instanceof ErroCampo)) throw erro;
      erros.push(erro.message);
    }
  }
  return { filtros, erros };
}

/** Mostra os filtros no formulario. Campo que nao veio fica vazio (desligado). */
function escreverFiltros(filtros) {
  for (const [campo, controle] of Object.entries(controles)) {
    controle.escrever(filtros ? filtros[campo] : undefined);
  }
}

/** Mostra (ou esconde, com texto vazio) a caixa de erro perto dos filtros. */
function mostrarErroFiltros(texto) {
  const caixa = porId("erro-filtros");
  caixa.textContent = texto || "";
  caixa.hidden = !texto;
}

// --- Envio da sessao ao servidor ---------------------------------------------
//
// Toda mudanca de filtro, modo ou comparacao manda a sessao INTEIRA (os tres campos) no PUT.
// Mudanca de filtro com um grupo escolhido tambem salva o grupo. Os envios nunca se atropelam:
// se mudar algo durante um envio, outro envio sai logo depois, com o formulario daquele momento.

let temporizadorEnvio = null;
let enviando = false;
let enviarDeNovo = false;
let salvarGrupoPendente = false;  // houve mudanca de filtro ainda nao salva no grupo
let semConexaoNoEnvio = false;    // o ultimo envio nao chegou ao scanner (sem conexao): reenviar ao reconectar

/** Um filtro mudou: manda depois de `atraso` ms (campos de texto esperam o dono parar de digitar). */
function aoMudarFiltro(atraso) {
  salvarGrupoPendente = true;
  agendarEnvio(atraso);
}

/** Agenda o envio da sessao (um novo agendamento substitui o anterior). */
function agendarEnvio(atraso) {
  clearTimeout(temporizadorEnvio);
  temporizadorEnvio = setTimeout(() => {
    temporizadorEnvio = null;
    enviarSessao();
  }, atraso);
}

/** Manda a sessao; se ja houver um envio em andamento, marca para mandar de novo quando ele acabar. */
async function enviarSessao() {
  if (enviando) {
    enviarDeNovo = true;
    return;
  }
  enviando = true;
  try {
    do {
      enviarDeNovo = false;
      await enviarSessaoUmaVez();
    } while (enviarDeNovo);
  } finally {
    enviando = false;
  }
}

async function enviarSessaoUmaVez() {
  const { filtros, erros } = lerFiltros();
  if (erros.length) {
    mostrarErroFiltros("Corrija antes de aplicar:\n" + erros.join("\n"));
    return;
  }
  const salvarGrupo = salvarGrupoPendente;
  salvarGrupoPendente = false;
  const grupo = estado.grupoSelecionado;
  try {
    estado.sessao = await pedir("PUT", "/api/sessao", {
      modo: estado.modo, filtros, comparacao: estado.comparacao,
    });
    if (salvarGrupo && grupo) {
      estado.grupos[grupo] = await pedir("POST", "/api/filtros/" + encodeURIComponent(grupo), filtros);
    }
    semConexaoNoEnvio = false;
    mostrarErroFiltros("");
  } catch (erro) {
    if (salvarGrupo) salvarGrupoPendente = true;  // tenta salvar no grupo de novo no proximo envio
    semConexaoNoEnvio = erro.status === 0;
    mostrarErroFiltros(semConexaoNoEnvio
      ? "Sem conexão com o scanner: os filtros serão enviados quando a conexão voltar."
      : "O scanner recusou os filtros: " + erro.message);
  }
  atualizarCabecalhos();
}

/** Se ha um envio esperando (digitacao) ou em andamento, termina ele agora. */
async function terminarEnvioPendente() {
  if (temporizadorEnvio !== null) {
    clearTimeout(temporizadorEnvio);
    temporizadorEnvio = null;
    await enviarSessao();
  }
  while (enviando) await esperar(30);
}

/**
 * Traz do servidor a sessao em uso e os grupos salvos e mostra no formulario (ao abrir a pagina e a
 * cada reconexao: se o scanner reiniciou, a sessao dele voltou ao padrao).
 * Se o grupo escolhido (ou lembrado neste navegador) existe e tem filtros diferentes, ele e aplicado de novo.
 * Se uma mudanca do dono nao chegou ao scanner por falta de conexao, ela e mandada agora (o formulario fica).
 */
async function sincronizarComServidor() {
  await terminarEnvioPendente();
  const [sessao, grupos] = await Promise.all([pedir("GET", "/api/sessao"), pedir("GET", "/api/filtros")]);
  estado.sessao = sessao;
  estado.grupos = grupos || {};
  const preferido = estado.grupoSelecionado || lerGuardado(GUARDADO_GRUPO);
  estado.grupoSelecionado = preferido && Object.prototype.hasOwnProperty.call(estado.grupos, preferido) ? preferido : "";
  if (semConexaoNoEnvio) {
    preencherGrupos();
    agendarEnvio(0);
    return;
  }
  estado.modo = sessao.modo;
  estado.comparacao = sessao.comparacao === true;
  escreverFiltros(sessao.filtros);
  mostrarErroFiltros("");

  if (estado.grupoSelecionado) {
    const filtrosDoGrupo = estado.grupos[estado.grupoSelecionado];
    if (textoEstavel(filtrosDoGrupo) !== textoEstavel(sessao.filtros)) {
      escreverFiltros(filtrosDoGrupo);
      agendarEnvio(0);
    }
  }
  preencherGrupos();
  atualizarControlesDeSessao();
  desenharAgora();
}

// --- Modo e comparacao ---------------------------------------------------------

/** Mostra na tela o modo e a comparacao escolhidos. */
function atualizarControlesDeSessao() {
  for (const botao of document.querySelectorAll("[data-modo]")) {
    botao.setAttribute("aria-pressed", String(botao.dataset.modo === estado.modo));
  }
  porId("comparacao").checked = estado.comparacao;
  porId("faixa-comparacao").hidden = !estado.comparacao;
  atualizarCabecalhos();
}

function prepararSessao() {
  for (const botao of document.querySelectorAll("[data-modo]")) {
    botao.addEventListener("click", () => {
      if (estado.modo === botao.dataset.modo) return;
      estado.modo = botao.dataset.modo;
      atualizarControlesDeSessao();
      agendarEnvio(0);
      desenharAgora();
    });
  }
  porId("comparacao").addEventListener("change", (evento) => {
    estado.comparacao = evento.target.checked;
    atualizarControlesDeSessao();
    agendarEnvio(0);
    desenharAgora();
  });

  // Recolher/abrir o bloco de filtros (lembrado neste navegador).
  const botaoFiltros = porId("botao-filtros");
  const aplicarRecolhido = (recolhido) => {
    porId("filtros").classList.toggle("recolhido", recolhido);
    botaoFiltros.setAttribute("aria-expanded", String(!recolhido));
  };
  aplicarRecolhido(lerGuardado(GUARDADO_FILTROS_RECOLHIDOS) === "1");
  botaoFiltros.addEventListener("click", () => {
    const recolhido = !porId("filtros").classList.contains("recolhido");
    aplicarRecolhido(recolhido);
    guardar(GUARDADO_FILTROS_RECOLHIDOS, recolhido ? "1" : null);
  });
}

// ============================================================================
// 6. Grupos de filtros (criar, renomear, duplicar, apagar)
// ============================================================================

let acaoDoGrupo = null;  // "criar" | "renomear" | "duplicar" enquanto o campo do nome esta aberto

/** Atualiza a lista de grupos e os botoes (renomear, duplicar e apagar precisam de um grupo escolhido). */
function preencherGrupos() {
  const caixa = porId("grupo");
  caixa.replaceChildren(new Option("(sem grupo)", ""));
  const nomes = Object.keys(estado.grupos).sort((a, b) => a.localeCompare(b, "pt-BR"));
  for (const nome of nomes) caixa.appendChild(new Option(nome, nome));
  caixa.value = estado.grupoSelecionado;
  const semGrupo = !estado.grupoSelecionado;
  porId("grupo-editar").disabled = semGrupo;
  porId("grupo-duplicar").disabled = semGrupo;
  porId("grupo-apagar").disabled = semGrupo;
}

/** Mensagem ao lado dos grupos (erro em vermelho). */
function mensagemGrupo(texto, erro) {
  const caixa = porId("grupo-mensagem");
  caixa.textContent = texto || "";
  caixa.classList.toggle("erro", Boolean(erro));
}

/** Escolhe um grupo: carrega os filtros dele no formulario e aplica no scanner. "" = nenhum grupo. */
async function escolherGrupo(nome) {
  await terminarEnvioPendente();  // o que foi digitado vai para o grupo anterior antes de trocar
  fecharCaixasDoGrupo();
  estado.grupoSelecionado = nome;
  guardar(GUARDADO_GRUPO, nome);
  preencherGrupos();
  mensagemGrupo("");
  if (nome && estado.grupos[nome]) {
    escreverFiltros(estado.grupos[nome]);
    salvarGrupoPendente = false;
    agendarEnvio(0);
    desenharAgora();
  }
}

/** Traz a lista de grupos do servidor de novo (depois de criar, renomear ou apagar). */
async function recarregarGrupos() {
  try {
    estado.grupos = (await pedir("GET", "/api/filtros")) || {};
  } catch (erro) {
    // fica com a lista que ja tinha; a mensagem do erro original ja foi mostrada
  }
  if (estado.grupoSelecionado && !(estado.grupoSelecionado in estado.grupos)) {
    estado.grupoSelecionado = "";
    guardar(GUARDADO_GRUPO, null);
  }
  preencherGrupos();
}

function fecharCaixasDoGrupo() {
  acaoDoGrupo = null;
  porId("grupo-formulario").hidden = true;
  porId("grupo-confirmacao").hidden = true;
}

/** Abre o campo do nome para criar, renomear ou duplicar. */
function abrirNomeDoGrupo(acao) {
  fecharCaixasDoGrupo();
  mensagemGrupo("");
  const atual = estado.grupoSelecionado;
  if (acao !== "criar" && !atual) return;
  acaoDoGrupo = acao;
  const rotulos = {
    criar: "Nome do novo grupo (começa com os filtros atuais)",
    renomear: `Novo nome para o grupo "${atual}"`,
    duplicar: `Nome da cópia do grupo "${atual}"`,
  };
  porId("grupo-formulario-rotulo").textContent = rotulos[acao];
  const campo = porId("grupo-nome");
  campo.value = acao === "criar" ? "" : acao === "renomear" ? atual : `${atual} (cópia)`;
  porId("grupo-formulario").hidden = false;
  campo.focus();
  campo.select();
}

/** Salva o nome digitado (criar, renomear ou duplicar). */
async function salvarNomeDoGrupo() {
  const acao = acaoDoGrupo;
  if (!acao) return;
  const nome = porId("grupo-nome").value.trim();
  const atual = estado.grupoSelecionado;
  if (!nome) return mensagemGrupo("Digite um nome para o grupo.", true);
  if (nome.includes("/")) return mensagemGrupo('O nome do grupo não pode ter "/".', true);
  if (acao === "renomear" && nome === atual) return fecharCaixasDoGrupo();
  await terminarEnvioPendente();
  if (Object.prototype.hasOwnProperty.call(estado.grupos, nome)) {
    return mensagemGrupo(`Já existe um grupo chamado "${nome}".`, true);
  }

  // Criar: comeca com os filtros do formulario. Renomear e duplicar: os filtros salvos no grupo.
  let filtros;
  if (acao === "criar") {
    const lidos = lerFiltros();
    if (lidos.erros.length) return mensagemGrupo("Corrija os filtros marcados em vermelho antes de criar o grupo.", true);
    filtros = lidos.filtros;
  } else {
    filtros = estado.grupos[atual];
  }

  try {
    await pedir("POST", "/api/filtros/" + encodeURIComponent(nome), filtros);
    if (acao === "renomear") await pedir("DELETE", "/api/filtros/" + encodeURIComponent(atual));
  } catch (erro) {
    mensagemGrupo("Não deu para salvar o grupo: " + erro.message, true);
    await recarregarGrupos();
    return;
  }
  fecharCaixasDoGrupo();
  estado.grupoSelecionado = nome;
  guardar(GUARDADO_GRUPO, nome);
  await recarregarGrupos();
  const feitos = { criar: "criado", renomear: "renomeado", duplicar: "duplicado" };
  mensagemGrupo(`Grupo "${nome}" ${feitos[acao]}.`);
}

/** Pede a confirmacao (dentro da pagina) para apagar o grupo escolhido. */
function pedirConfirmacaoApagar() {
  fecharCaixasDoGrupo();
  mensagemGrupo("");
  if (!estado.grupoSelecionado) return;
  porId("grupo-confirmacao-texto").textContent =
    `Apagar o grupo "${estado.grupoSelecionado}"? Os filtros em uso continuam valendo até você mudar.`;
  porId("grupo-confirmacao").hidden = false;
  porId("grupo-cancelar-apagar").focus();
}

async function apagarGrupo() {
  const nome = estado.grupoSelecionado;
  fecharCaixasDoGrupo();
  if (!nome) return;
  await terminarEnvioPendente();
  try {
    await pedir("DELETE", "/api/filtros/" + encodeURIComponent(nome));
  } catch (erro) {
    mensagemGrupo("Não deu para apagar o grupo: " + erro.message, true);
    await recarregarGrupos();
    return;
  }
  estado.grupoSelecionado = "";
  guardar(GUARDADO_GRUPO, null);
  await recarregarGrupos();
  mensagemGrupo(`Grupo "${nome}" apagado.`);
}

function prepararGrupos() {
  porId("grupo").addEventListener("change", (evento) => escolherGrupo(evento.target.value));
  porId("grupo-criar").addEventListener("click", () => abrirNomeDoGrupo("criar"));
  porId("grupo-editar").addEventListener("click", () => abrirNomeDoGrupo("renomear"));
  porId("grupo-duplicar").addEventListener("click", () => abrirNomeDoGrupo("duplicar"));
  porId("grupo-apagar").addEventListener("click", pedirConfirmacaoApagar);
  porId("grupo-salvar").addEventListener("click", salvarNomeDoGrupo);
  porId("grupo-cancelar").addEventListener("click", fecharCaixasDoGrupo);
  porId("grupo-confirmar-apagar").addEventListener("click", apagarGrupo);
  porId("grupo-cancelar-apagar").addEventListener("click", fecharCaixasDoGrupo);
  porId("grupo-nome").addEventListener("keydown", (evento) => {
    if (evento.key === "Enter") salvarNomeDoGrupo();
    if (evento.key === "Escape") fecharCaixasDoGrupo();
  });
  preencherGrupos();
}

// ============================================================================
// 7. WebSocket /ws/sinais (tabela ao vivo)
// ============================================================================
//
// Mensagens do servidor:
//   {"tipo": "snapshot", "sinais": [...]}       substitui a tabela inteira (ao conectar)
//   {"tipo": "novo" | "atualizado", "sinal": {...}}  poe ou troca o sinal pela chave
//   {"tipo": "encerrado", "sinal": {...}}        tira o sinal
// Caiu a conexao: tenta de novo em 1 s, 2 s, 4 s... ate 30 s. (Nao da para saber pelo codigo de
// fechamento se foi senha errada: no servidor de verdade isso chega como uma falha comum.)

let conexao = null;
let esperaReconexaoMs = RECONEXAO_INICIAL_MS;

function conectar() {
  const protocolo = window.location.protocol === "https:" ? "wss://" : "ws://";
  let socket;
  try {
    socket = new WebSocket(protocolo + window.location.host + "/ws/sinais");
  } catch (erro) {
    agendarReconexao();
    return;
  }
  conexao = socket;
  socket.addEventListener("open", () => {
    estado.conexao = "ao_vivo";
    estado.proximaTentativaMs = null;
    esperaReconexaoMs = RECONEXAO_INICIAL_MS;
    atualizarBarra();
    sincronizarComServidor().catch((erro) => mostrarErroFiltros("Não deu para ler os filtros do scanner: " + erro.message));
  });
  socket.addEventListener("message", (evento) => receberMensagem(evento.data));
  socket.addEventListener("close", () => {
    if (conexao !== socket) return;
    conexao = null;
    agendarReconexao();
  });
}

function agendarReconexao() {
  estado.conexao = "reconectando";
  estado.proximaTentativaMs = Date.now() + esperaReconexaoMs;
  setTimeout(conectar, esperaReconexaoMs);
  esperaReconexaoMs = Math.min(esperaReconexaoMs * 2, RECONEXAO_MAXIMA_MS);
  atualizarBarra();
  agendarDesenho();
}

/** Aplica uma mensagem do WebSocket ao estado (rapido) e agenda o redesenho (devagar, junta varias). */
function receberMensagem(texto) {
  let mensagem;
  try {
    mensagem = JSON.parse(texto);
  } catch (erro) {
    return;
  }
  estado.ultimaMensagemMs = Date.now();
  if (mensagem.tipo === "snapshot") {
    const novos = new Map();
    for (const sinal of mensagem.sinais || []) novos.set(chaveDoSinal(sinal), sinal);
    estado.sinais = novos;
  } else if (mensagem.tipo === "novo" || mensagem.tipo === "atualizado") {
    estado.sinais.set(chaveDoSinal(mensagem.sinal), mensagem.sinal);
  } else if (mensagem.tipo === "encerrado") {
    estado.sinais.delete(chaveDoSinal(mensagem.sinal));
  }
  agendarDesenho();
}

// ============================================================================
// 8. Tabela de sinais
// ============================================================================

let temporizadorDesenho = null;
let ultimoDesenhoMs = 0;
const linhasDaTabela = new Map();  // chave -> {tr, sinal, fixado, visao}: reaproveita as linhas que nao mudaram

// Linhas perto da parte visivel da pagina. So o conteudo delas e refeito a cada redesenho; as outras
// continuam na posicao certa e sao atualizadas assim que aparecem na tela (rolando a pagina).
// Sem isso, centenas de sinais atualizados a cada ciclo deixariam o navegador lento.
const linhasNaTela = new Set();
const observadorDeLinhas = "IntersectionObserver" in window
  ? new IntersectionObserver((entradas) => {
    let apareceu = false;
    for (const entrada of entradas) {
      const chave = entrada.target.dataset.chave;
      if (entrada.isIntersecting) {
        linhasNaTela.add(chave);
        apareceu = true;
      } else {
        linhasNaTela.delete(chave);
      }
    }
    if (apareceu) agendarDesenho();
  }, { rootMargin: "600px 0px" })
  : null;

/** Redesenha logo que der, mas no maximo a cada INTERVALO_DESENHO_MS. */
function agendarDesenho() {
  if (temporizadorDesenho !== null) return;
  const espera = Math.max(0, INTERVALO_DESENHO_MS - (performance.now() - ultimoDesenhoMs));
  temporizadorDesenho = setTimeout(desenharAgora, espera);
}

/** Redesenha ja (depois de um clique do dono). */
function desenharAgora() {
  clearTimeout(temporizadorDesenho);
  temporizadorDesenho = null;
  ultimoDesenhoMs = performance.now();
  desenharTabela();
  atualizarBarra();
}

function desenharTabela() {
  const fonte = estado.congelados || estado.sinais;
  const fixados = new Set(estado.fixados.map(String));
  const criterio = controles.ordenar_por ? controles.ordenar_por.ler() : null;
  const visao = `${estado.modo}|${estado.comparacao}`;  // o que muda o desenho de todas as linhas

  const lista = [];
  for (const [chave, sinal] of fonte) {
    if (passaNaBusca(sinal, estado.busca)) lista.push({ chave, sinal, fixado: fixados.has(chave) });
  }
  ordenar(lista, criterio);

  const corpo = porId("corpo-tabela");
  const visiveis = new Set(lista.map((item) => item.chave));
  for (const [chave, linha] of linhasDaTabela) {
    if (!visiveis.has(chave)) {
      if (observadorDeLinhas) observadorDeLinhas.unobserve(linha.tr);
      linhasNaTela.delete(chave);
      linha.tr.remove();
      linhasDaTabela.delete(chave);
    }
  }
  lista.forEach((item, posicao) => {
    let linha = linhasDaTabela.get(item.chave);
    if (!linha) {
      linha = { tr: montarLinha(item), sinal: item.sinal, fixado: item.fixado, visao };
      linhasDaTabela.set(item.chave, linha);
      if (observadorDeLinhas) observadorDeLinhas.observe(linha.tr);
    } else if ((linha.sinal !== item.sinal || linha.fixado !== item.fixado || linha.visao !== visao) &&
               (!observadorDeLinhas || linhasNaTela.has(item.chave))) {
      // Monta a linha nova fora da pagina e copia para a linha que ja esta na tela so o que mudou
      // (com centenas de sinais por ciclo, trocar a linha inteira deixaria o navegador lento).
      igualar(linha.tr, montarLinha(item));
      linha.sinal = item.sinal;
      linha.fixado = item.fixado;
      linha.visao = visao;
    }
    const naPosicao = corpo.children[posicao];
    if (naPosicao !== linha.tr) corpo.insertBefore(linha.tr, naPosicao || null);
  });

  estado.visiveis = lista.length;
  estado.total = fonte.size;
  const vazia = porId("tabela-vazia");
  if (lista.length) {
    vazia.hidden = true;
  } else {
    vazia.hidden = false;
    if (estado.conexao === "conectando") vazia.textContent = "Conectando ao scanner…";
    else if (estado.conexao === "reconectando" && !fonte.size) vazia.textContent = "Sem conexão com o scanner. Tentando de novo…";
    else if (fonte.size && estado.busca) vazia.textContent = `Nenhuma moeda com "${estado.busca}".`;
    else vazia.textContent = "Nenhuma oportunidade com os filtros atuais no momento.";
  }
  porId("contorno-tabela").classList.toggle("desatualizada", estado.conexao !== "ao_vivo");
  atualizarCabecalhos();  // o periodo do "Historico (4h)" pode vir dos sinais
}

/** Monta uma linha da tabela (fora da pagina). */
function montarLinha(item) {
  const { sinal, fixado } = item;
  const tr = document.createElement("tr");
  tr.dataset.chave = item.chave;
  if (fixado) tr.className = "fixada";
  tr.append(
    celulaMoeda(sinal),
    celulaPonta(sinal.ponta_long, "long"),
    celulaPonta(sinal.ponta_short, "short"),
    celulaResultado(sinal),
    celulaHistorico(sinal),
    celulaFixar(fixado),
  );
  return tr;
}

/**
 * Deixa o elemento `destino` (que esta na pagina) igual ao `modelo` (recem-montado, fora da pagina),
 * mexendo o minimo: troca so os textos e atributos diferentes, e so troca um elemento inteiro quando
 * ele e de outro tipo. A tabela nao usa eventos em cada elemento (um so ouvinte no corpo), entao nada se perde.
 */
function igualar(destino, modelo) {
  if (destino.nodeType === Node.TEXT_NODE) {
    if (destino.nodeValue !== modelo.nodeValue) destino.nodeValue = modelo.nodeValue;
    return;
  }
  for (const { name, value } of modelo.attributes) {
    if (destino.getAttribute(name) !== value) destino.setAttribute(name, value);
  }
  for (const { name } of [...destino.attributes]) {
    if (!modelo.hasAttribute(name)) destino.removeAttribute(name);
  }
  const filhosDoModelo = [...modelo.childNodes];
  filhosDoModelo.forEach((filho, indice) => {
    const atual = destino.childNodes[indice];
    if (!atual) destino.appendChild(filho);
    else if (atual.nodeType === filho.nodeType && atual.nodeName === filho.nodeName) igualar(atual, filho);
    else destino.replaceChild(filho, atual);
  });
  while (destino.childNodes.length > filhosDoModelo.length) destino.lastChild.remove();
}

/** Um pedaco "ROTULO: valor" (o valor em negrito, com cor opcional). */
function pedaco(rotulo, valor, classe) {
  const span = criar("span", null, rotulo + " ");
  span.appendChild(criar("b", classe || null, valor));
  return span;
}

function celulaMoeda(sinal) {
  const td = criar("td", "col-moeda");
  const [base, cotacao] = String(sinal.par || "").split("/");
  const linha1 = criar("div", "moeda-linha");
  linha1.append(criar("span", "moeda-base", base), criar("span", "moeda-cotacao", cotacao || ""));
  for (const etiqueta of sinal.etiquetas || []) {
    const texto = String(etiqueta);
    linha1.appendChild(criar("span", texto.toUpperCase() === "STOCK" ? "etiqueta etiqueta-stock" : "etiqueta etiqueta-outra", texto));
  }
  if (sinal.suspeito) {
    const marca = criar("span", "etiqueta etiqueta-suspeito", "SUSPEITO");
    marca.title = "Spread de abertura acima do limite: pode ser outro token com o mesmo nome";
    linha1.appendChild(marca);
  }
  if (sinal.aviso_cotacao) {
    const marca = criar("span", "etiqueta etiqueta-cotacao", "COTAÇÃO 1:1");
    marca.title = "Sem cotação USDC/USDG: o preço foi convertido 1:1 para USDT";
    linha1.appendChild(marca);
  }
  const tipo = criar("div", "tipo tipo-" + sinal.tipo, NOMES_TIPOS[sinal.tipo] || sinal.tipo);
  const desde = criar("div", "menor", "desde " + horaBrasilia(Date.parse(sinal.detectado_em)));
  desde.title = "Detectado em " + diaHoraBrasilia(Date.parse(sinal.detectado_em)) + " (Brasília)";
  td.append(linha1, tipo, desde);
  return td;
}

function celulaPonta(ponta, lado) {
  const td = criar("td", "col-ponta");
  if (!ponta) return td;
  const linha1 = criar("div", "ponta-linha1");
  linha1.append(
    criar("span", "corretora", nomeCorretora(ponta.corretora)),
    criar("span", "mercado", NOMES_MERCADOS[ponta.mercado] || ponta.mercado),
    criar("span", "preco", preco(ponta.preco)),
    criar("span", "lado " + (lado === "long" ? "positivo" : "negativo"), lado === "long" ? "(Long)" : "(Short)"),
    criar("span", "lote", usdCompacto(ponta.lote_usd)),
    criar("span", "lote-medio", "(" + usdCompacto(ponta.lote_medio_usd) + ")"),
  );
  linha1.title = `Símbolo na corretora: ${ponta.simbolo_original} · taxa taker ${formatoPctSemSinal.format(ponta.taxa_taker_pct || 0)}%` +
    " · lote agora (lote médio do período)";

  const linha2 = criar("div", "ponta-linha2");
  linha2.appendChild(pedaco("VOL:", usdCompacto(ponta.volume_24h_usd)));
  // Na ponta spot nao ha funding: aparece "—".
  const temFunding = ponta.mercado === "futuro" && numeroValido(ponta.funding_pct);
  const fr = pedaco("FR:", temFunding ? pct(ponta.funding_pct) : "—", temFunding ? classeSinal(ponta.funding_pct) : null);
  if (temFunding && numeroValido(ponta.funding_intervalo_h)) {
    fr.appendChild(document.createTextNode(` (${formatoUmaCasa.format(ponta.funding_intervalo_h)}h)`));
  }
  linha2.append(fr, pedaco("MAX:", usdCompacto(ponta.limite_ordem_usd)), pedaco("VAR:", pct(ponta.variacao_24h_pct)));
  td.append(linha1, linha2);
  return td;
}

function celulaResultado(sinal) {
  const td = criar("td", "col-resultado");
  const spreads = criar("div", "spreads");
  for (const [rotulo, valor] of [["A", sinal.spread_abertura_pct], ["F", sinal.spread_fechamento_pct]]) {
    const span = criar("span", classeSinal(valor));
    span.append(criar("span", "rotulo-curto", rotulo), document.createTextNode(pct(valor)));
    spreads.appendChild(span);
  }
  const yieldFr = criar("div", "menor");
  yieldFr.appendChild(pedaco("FR Yield:", pct(sinal.fr_yield_8h_pct), classeSinal(sinal.fr_yield_8h_pct)));
  yieldFr.title = "Funding das duas pontas normalizado para 8h";

  // Destaque: RP liquido; no modo comparacao, o RP bruto (sem custos, como o Arbimax).
  const linhaRp = criar("div", "linha-rp");
  const destaqueBruto = estado.comparacao;
  const principal = destaqueBruto ? sinal.rp_bruto_pct : sinal.rp_liquido_pct;
  const secundario = destaqueBruto ? sinal.rp_liquido_pct : sinal.rp_bruto_pct;
  const destaque = criar("span", "rp-destaque " + classeSinal(principal), (destaqueBruto ? "RP bruto " : "RP líq. ") + pct(principal));
  destaque.title = destaqueBruto
    ? "RP bruto = A atual + F da métrica (sem taxas, slippage e funding)"
    : `RP líquido = RP bruto − taxas (${pct(sinal.custo_taxas_pct)}) + funding esperado (${pct(sinal.funding_esperado_pct)})`;
  linhaRp.append(destaque, criar("span", "rp-secundario", (destaqueBruto ? "líq. " : "bruto ") + pct(secundario)));
  td.append(spreads, yieldFr, linhaRp);

  // Modo Farm: custo para gerar $1.000 de volume (positivo = gasta, negativo = ganha).
  if (numeroValido(sinal.custo_por_1000_usd)) {
    const custo = criar("div", "custo");
    const classe = sinal.custo_por_1000_usd > 0 ? "negativo" : sinal.custo_por_1000_usd < 0 ? "positivo" : "neutro";
    custo.appendChild(pedaco("Custo a cada $1.000:", usd(sinal.custo_por_1000_usd), classe));
    td.appendChild(custo);
  }
  return td;
}

function celulaHistorico(sinal) {
  const td = criar("td", "col-historico");
  const metricas = sinal.metricas_periodo;
  const linha1 = criar("div", "historico-linha1");
  const invertidas = metricas && numeroValido(metricas.invertidas) ? metricas.invertidas : null;
  const textoInvertidas = criar("span");
  textoInvertidas.append(criar("b", null, invertidas === null ? "—" : formatoInteiro.format(invertidas)),
    document.createTextNode(invertidas === 1 ? " invertida" : " invertidas"));
  const botao = criar("button", "botao-icone botao-grafico");
  botao.type = "button";
  botao.dataset.acao = "grafico";
  botao.title = "Abrir o gráfico do histórico";
  botao.setAttribute("aria-label", "Abrir o gráfico do histórico de " + sinal.par);
  botao.appendChild(icone("grafico"));
  linha1.append(textoInvertidas, botao);

  const linha2 = criar("div", "menor");
  const aMedio = metricas ? metricas.abertura_media_pct : null;
  const fMedio = metricas ? metricas.fechamento_medio_pct : null;
  linha2.append(pedaco("A", pct(aMedio), classeSinal(aMedio)), document.createTextNode("  "),
    pedaco("F", pct(fMedio), classeSinal(fMedio)));
  linha2.title = "A e F médios do período";
  td.append(linha1, linha2);
  return td;
}

function celulaFixar(fixado) {
  const td = criar("td", "col-fixar");
  const botao = criar("button", "botao-icone botao-fixar");
  botao.type = "button";
  botao.dataset.acao = "fixar";
  botao.setAttribute("aria-pressed", String(fixado));
  botao.title = fixado ? "Desafixar" : "Fixar no topo";
  botao.setAttribute("aria-label", botao.title);
  botao.appendChild(icone("fixar"));
  td.appendChild(botao);
  return td;
}

/** Fixa ou desafixa um sinal (vai para os filtros: e salvo na sessao e no grupo). */
function alternarFixado(chave) {
  const indice = estado.fixados.map(String).indexOf(chave);
  if (indice >= 0) estado.fixados.splice(indice, 1);
  else estado.fixados.push(chave);
  if (controles.fixados) controles.fixados.atualizar();
  aoMudarFiltro(0);
  desenharAgora();
}

/** Titulos das colunas que dependem do modo e do periodo. */
function atualizarCabecalhos() {
  const resultado = estado.comparacao ? "Resultado (comparação: RP bruto)" : estado.modo === "farm" ? "Resultado (Farm)" : "Resultado";
  const periodo = periodoEmUso();
  const historico = periodo ? `Histórico (${formatoUmaCasa.format(periodo)}h)` : "Histórico";
  trocarTexto(porId("cabecalho-resultado"), resultado);
  trocarTexto(porId("cabecalho-historico"), historico);
}

/** Troca o texto de um elemento so se mudou (nao mexe na pagina a toa). */
function trocarTexto(elemento, texto) {
  if (elemento.textContent !== texto) elemento.textContent = texto;
}

/** O periodo das metricas: o dos filtros, se preenchido; senao o que veio nos sinais. */
function periodoEmUso() {
  const filtros = estado.sessao && estado.sessao.filtros;
  if (filtros && numeroValido(filtros.periodo_metricas_h) && filtros.periodo_metricas_h > 0) return filtros.periodo_metricas_h;
  for (const sinal of estado.sinais.values()) {
    if (sinal.metricas_periodo && numeroValido(sinal.metricas_periodo.periodo_h)) return sinal.metricas_periodo.periodo_h;
  }
  return null;
}

function prepararTabela() {
  porId("corpo-tabela").addEventListener("click", (evento) => {
    const botao = evento.target.closest("[data-acao]");
    if (!botao) return;
    const chave = botao.closest("tr").dataset.chave;
    if (botao.dataset.acao === "fixar") alternarFixado(chave);
    else if (botao.dataset.acao === "grafico") abrirGrafico(chave, botao);
  });
}

// ============================================================================
// 9. Ordenacao e busca
// ============================================================================

/** Numero para ordenar (maior vem primeiro); sem valor vai para o fim. */
function valorParaOrdenar(sinal, criterio) {
  const ou = (valor) => (numeroValido(valor) ? valor : -Infinity);
  switch (criterio) {
    case "horario": {
      const momento = Date.parse(sinal.detectado_em);
      return Number.isFinite(momento) ? momento : -Infinity;
    }
    case "spread":
      return ou(sinal.spread_abertura_pct);
    case "volume": {
      const long = sinal.ponta_long || {};
      const short = sinal.ponta_short || {};
      return Math.min(ou(long.volume_24h_usd), ou(short.volume_24h_usd));  // o menor volume das duas pontas
    }
    default:  // "resultado" (ou vazio)
      if (estado.comparacao) return ou(sinal.rp_bruto_pct);
      if (estado.modo === "farm") return numeroValido(sinal.custo_por_1000_usd) ? -sinal.custo_por_1000_usd : -Infinity;
      return ou(sinal.rp_liquido_pct);
  }
}

/** Fixados primeiro; dentro de cada parte, pelo criterio; empate pela chave (a ordem nao fica pulando). */
function ordenar(lista, criterio) {
  for (const item of lista) item.ordem = valorParaOrdenar(item.sinal, criterio);
  lista.sort((a, b) => {
    if (a.fixado !== b.fixado) return a.fixado ? -1 : 1;
    if (a.ordem !== b.ordem) return a.ordem > b.ordem ? -1 : 1;
    return a.chave < b.chave ? -1 : a.chave > b.chave ? 1 : 0;
  });
}

/** A busca olha so a moeda (o "BTC" de "BTC/USDT"), para "USD" nao achar todos os pares em USDT. */
function passaNaBusca(sinal, busca) {
  return !busca || String(sinal.par || "").split("/")[0].toUpperCase().includes(busca);
}

// ============================================================================
// 10. Barra de status (rodape)
// ============================================================================

function atualizarBarra() {
  porId("ultima-atualizacao").textContent = estado.ultimaMensagemMs ? horaBrasilia(estado.ultimaMensagemMs, true) : "—";
  const total = estado.total;
  porId("contagem").textContent = estado.visiveis === total
    ? `${total} ${total === 1 ? "oportunidade" : "oportunidades"}`
    : `${estado.visiveis} de ${total} oportunidades`;

  const caixa = porId("conexao");
  caixa.className = "conexao conexao-" + estado.conexao;
  let texto = "Conectando…";
  if (estado.conexao === "ao_vivo") texto = "Ao vivo";
  else if (estado.conexao === "reconectando") {
    const segundos = estado.proximaTentativaMs ? Math.max(0, Math.ceil((estado.proximaTentativaMs - Date.now()) / 1000)) : 0;
    texto = segundos > 0 ? `Sem conexão: tentando de novo em ${segundos}s` : "Reconectando…";
  }
  porId("conexao-texto").textContent = texto;
  porId("aviso-pausa").hidden = !estado.congelados;
}

function desenharSaude() {
  const caixa = porId("saude-corretoras");
  caixa.replaceChildren();
  if (estado.saudeFalhou && !estado.saude) {
    caixa.appendChild(criar("span", "apagado", "saúde das corretoras indisponível"));
    return;
  }
  for (const [corretora, dados] of Object.entries(estado.saude || {})) {
    const situacao = dados && TEXTOS_SAUDE[dados.estado] ? dados.estado : "desconhecida";
    const item = criar("span", "saude saude-" + situacao);
    item.append(document.createElement("i"), criar("span", "saude-nome", nomeCorretora(corretora)));
    if (situacao !== "conectada") item.appendChild(criar("span", "saude-estado", TEXTOS_SAUDE[situacao] || String(dados && dados.estado)));
    const partes = [`${nomeCorretora(corretora)}: ${TEXTOS_SAUDE[situacao] || dados.estado}`];
    if (dados && numeroValido(dados.atraso_medio_ms)) partes.push(`atraso médio ${formatoInteiro.format(dados.atraso_medio_ms)} ms`);
    if (dados && numeroValido(dados.reconexoes)) partes.push(`${dados.reconexoes} reconexões`);
    if (estado.saudeFalhou) partes.push("(sem resposta agora; mostrando a última)");
    item.title = partes.join(" · ");
    caixa.appendChild(item);
  }
}

/** Pergunta a saude das corretoras a cada INTERVALO_STATUS_MS (a lista tambem alimenta as opcoes de corretoras). */
async function acompanharSaude() {
  try {
    estado.saude = await pedir("GET", "/api/status");
    estado.saudeFalhou = false;
    const nomes = Object.keys(estado.saude || {});
    if (textoEstavel(nomes) !== textoEstavel(estado.corretoras)) {
      estado.corretoras = nomes;
      for (const lista of listasMulti) lista.atualizarOpcoes();
    }
  } catch (erro) {
    estado.saudeFalhou = true;
  }
  desenharSaude();
  setTimeout(acompanharSaude, INTERVALO_STATUS_MS);
}

function prepararBarra() {
  // Pausar: a tabela fica parada na foto de agora; os eventos continuam chegando e entrando no estado.
  porId("pausar").addEventListener("click", () => {
    const pausar = !estado.congelados;
    estado.congelados = pausar ? new Map(estado.sinais) : null;
    const botao = porId("pausar");
    botao.textContent = pausar ? "Continuar" : "Pausar";
    botao.setAttribute("aria-pressed", String(pausar));
    desenharAgora();
  });
  porId("busca").addEventListener("input", (evento) => {
    estado.busca = evento.target.value.trim().toUpperCase();
    desenharAgora();
  });
  setInterval(atualizarBarra, 1000);  // a contagem regressiva da reconexao
}

// ============================================================================
// 11. Grafico do historico
// ============================================================================
//
// Duas linhas (preco da ponta long e da ponta short) no periodo, com a biblioteca Lightweight Charts.
// O tempo do grafico e em segundos inteiros, crescentes e sem repetir: as amostras sao ordenadas e
// fica a ultima de cada segundo. Os horarios aparecem em Brasilia (formatadores proprios).
// Limitacao conhecida: as amostras guardam so o ask do long e o bid do short, entao as linhas de
// preco sao as de abertura nas duas intencoes; o que muda entre abertura e fechamento e o spread.

const grafico = {
  chave: null,
  sinal: null,
  intencao: "abertura",
  instancia: null,     // o grafico da biblioteca (null quando fechado)
  pontos: [],
  mediaAteOFim: [],    // spread medio de cada ponto ate o ultimo
  indicePorTempo: new Map(),
  pedido: 0,           // numero do ultimo pedido (resposta velha e ignorada)
  botaoDeOrigem: null,
};

function rotuloPonta(ponta) {
  return ponta ? `${nomeCorretora(ponta.corretora)} ${NOMES_MERCADOS[ponta.mercado] || ponta.mercado}` : "";
}

function abrirGrafico(chave, botao) {
  const sinal = (estado.congelados && estado.congelados.get(chave)) || estado.sinais.get(chave);
  if (!sinal) return;
  grafico.chave = chave;
  grafico.sinal = sinal;
  grafico.intencao = "abertura";
  grafico.botaoDeOrigem = botao || null;
  porId("grafico-titulo").textContent = `${sinal.par} — ${NOMES_TIPOS[sinal.tipo] || sinal.tipo}`;
  porId("grafico-subtitulo").textContent =
    `Long: ${rotuloPonta(sinal.ponta_long)} · Short: ${rotuloPonta(sinal.ponta_short)}`;
  desenharLegenda();
  porId("modal-grafico").hidden = false;
  porId("grafico-fechar").focus();
  carregarGrafico();
}

function fecharGrafico() {
  if (porId("modal-grafico").hidden) return;
  grafico.pedido += 1;  // descarta respostas que ainda vao chegar
  destruirGrafico();
  porId("modal-grafico").hidden = true;
  if (grafico.botaoDeOrigem && document.body.contains(grafico.botaoDeOrigem)) grafico.botaoDeOrigem.focus();
}

function destruirGrafico() {
  if (grafico.instancia) {
    grafico.instancia.remove();
    grafico.instancia = null;
  }
  porId("grafico-dica").hidden = true;
}

function mensagemGrafico(texto) {
  const caixa = porId("grafico-mensagem");
  caixa.textContent = texto || "";
  caixa.hidden = !texto;
}

function desenharLegenda() {
  const caixa = porId("grafico-legenda");
  caixa.replaceChildren();
  const cores = coresDoGrafico();
  for (const [ponta, cor, lado] of [[grafico.sinal.ponta_long, cores.long, "long"], [grafico.sinal.ponta_short, cores.short, "short"]]) {
    const item = criar("span", "legenda-item");
    const linha = criar("span", "legenda-linha");
    linha.style.background = cor;
    item.append(linha, document.createTextNode(`${rotuloPonta(ponta)} (${lado})`));
    caixa.appendChild(item);
  }
}

function desenharResumo(resumo) {
  const caixa = porId("grafico-resumo");
  caixa.replaceChildren();
  const r = resumo || {};
  const itens = [
    ["Maior spread:", pct(r.maior_spread)],
    ["Média dos 10 maiores:", pct(r.media_10_maiores)],
    ["Spread médio:", pct(r.spread_medio)],
    [`Lote médio ${rotuloPonta(grafico.sinal.ponta_long)}:`, usdCompacto(r.lote_medio_long)],
    [`Lote médio ${rotuloPonta(grafico.sinal.ponta_short)}:`, usdCompacto(r.lote_medio_short)],
  ];
  for (const [rotulo, valor] of itens) caixa.appendChild(pedaco(rotulo, valor));
}

/** O periodo para pedir ao servidor: o dos filtros em uso, ou null (o servidor usa o padrao). */
function periodoDaSessao() {
  const filtros = estado.sessao && estado.sessao.filtros;
  return filtros && numeroValido(filtros.periodo_metricas_h) && filtros.periodo_metricas_h > 0 ? filtros.periodo_metricas_h : null;
}

async function carregarGrafico() {
  const numero = ++grafico.pedido;
  for (const botao of document.querySelectorAll("[data-intencao]")) {
    botao.setAttribute("aria-pressed", String(botao.dataset.intencao === grafico.intencao));
  }
  destruirGrafico();
  mensagemGrafico("Carregando o histórico…");
  const periodo = periodoDaSessao();
  const parametros = new URLSearchParams({ intencao: grafico.intencao });
  if (periodo) parametros.set("periodo_h", String(periodo));
  const periodoMostrado = periodo || (grafico.sinal.metricas_periodo && grafico.sinal.metricas_periodo.periodo_h);
  porId("grafico-periodo").textContent = numeroValido(periodoMostrado) ? `Período: ${formatoUmaCasa.format(periodoMostrado)}h` : "";

  let dados;
  try {
    dados = await pedir("GET", "/api/historico/" + encodeURIComponent(grafico.chave) + "?" + parametros.toString());
  } catch (erro) {
    if (numero === grafico.pedido) mensagemGrafico("Não deu para carregar o histórico: " + erro.message);
    return;
  }
  if (numero !== grafico.pedido) return;  // o dono fechou ou trocou a intencao enquanto esperava

  desenharResumo(dados.resumo);
  prepararPontos(dados.amostras || []);
  if (!grafico.pontos.length) {
    mensagemGrafico("Ainda não há amostras deste par no período.");
    return;
  }
  const biblioteca = await esperarBiblioteca();
  if (numero !== grafico.pedido) return;
  if (!biblioteca) {
    mensagemGrafico("Não deu para carregar a biblioteca do gráfico (Lightweight Charts, vem da internet). " +
      "Confira a conexão e abra o gráfico de novo. O resto do painel funciona normalmente.");
    return;
  }
  mensagemGrafico("");
  desenharGrafico(biblioteca);
}

/** Espera a biblioteca do grafico chegar (ela carrega sem travar a pagina). null se nao vier. */
async function esperarBiblioteca() {
  const inicio = Date.now();
  while (!window.LightweightCharts) {
    if (window.falhaBibliotecaGraficos || Date.now() - inicio > ESPERA_BIBLIOTECA_MS) return null;
    mensagemGrafico("Carregando a biblioteca do gráfico…");
    await esperar(200);
  }
  return window.LightweightCharts;
}

/** Ordena as amostras, deixa uma por segundo (a ultima) e calcula o spread medio de cada ponto ate o fim. */
function prepararPontos(amostras) {
  const ordenadas = amostras.filter((a) => numeroValido(a.ts)).slice().sort((a, b) => a.ts - b.ts);
  const pontos = [];
  for (const amostra of ordenadas) {
    const tempo = Math.floor(amostra.ts);
    const ponto = {
      tempo,
      spread: amostra.spread,
      precoLong: amostra.preco_long,
      precoShort: amostra.preco_short,
      loteLong: amostra.lote_long,
      loteShort: amostra.lote_short,
    };
    if (pontos.length && pontos[pontos.length - 1].tempo === tempo) pontos[pontos.length - 1] = ponto;
    else pontos.push(ponto);
  }
  // Somas acumuladas de tras para frente: media de i ate o fim = soma[i] / quantidade[i].
  const media = new Array(pontos.length);
  let soma = 0;
  let quantidade = 0;
  for (let i = pontos.length - 1; i >= 0; i -= 1) {
    if (numeroValido(pontos[i].spread)) {
      soma += pontos[i].spread;
      quantidade += 1;
    }
    media[i] = quantidade ? soma / quantidade : null;
  }
  grafico.pontos = pontos;
  grafico.mediaAteOFim = media;
  grafico.indicePorTempo = new Map(pontos.map((ponto, indice) => [ponto.tempo, indice]));
}

/** As cores do grafico vem das variaveis do estilo.css. */
function coresDoGrafico() {
  const estilo = getComputedStyle(document.documentElement);
  const cor = (nome, padrao) => estilo.getPropertyValue(nome).trim() || padrao;
  return {
    fundo: cor("--painel", "#0d0f13"),
    texto: cor("--texto-2", "#a0a6b0"),
    grade: cor("--borda", "#22262e"),
    borda: cor("--borda-forte", "#333844"),
    long: cor("--azul", "#4c9aff"),
    short: cor("--vermelho", "#f2545b"),
  };
}

/** Casas decimais para os precos do grafico, conforme o tamanho do menor preco. */
function casasDosPrecos(pontos) {
  let menor = Infinity;
  for (const p of pontos) {
    for (const valor of [p.precoLong, p.precoShort]) if (numeroValido(valor) && valor > 0) menor = Math.min(menor, valor);
  }
  if (!Number.isFinite(menor) || menor >= 1000) return 2;
  if (menor >= 1) return 4;
  return Math.min(10, Math.ceil(-Math.log10(menor)) + 3);
}

/** Texto das marcas do eixo do tempo, em Brasilia (tipos da biblioteca: 0 ano, 1 mes, 2 dia, 3 hora, 4 hora com segundos). */
function marcaDoEixo(tempo, tipoDaMarca) {
  const ms = Number(tempo) * 1000;
  const data = new Date(ms);
  switch (tipoDaMarca) {
    case 0: return formatoAno.format(data);
    case 1: return formatoMes.format(data);
    case 2: return formatoDiaMes.format(data);
    case 4: return horaBrasilia(ms);
    default: return horaMinutoBrasilia(ms);
  }
}

function desenharGrafico(LC) {
  destruirGrafico();
  const cores = coresDoGrafico();
  const casas = casasDosPrecos(grafico.pontos);
  const formatoDoGrafico = new Intl.NumberFormat("pt-BR", { minimumFractionDigits: Math.min(casas, 2), maximumFractionDigits: casas });
  const formatarPreco = (valor) => formatoDoGrafico.format(valor);

  const instancia = LC.createChart(porId("grafico-tela"), {
    autoSize: true,
    layout: { background: { type: "solid", color: cores.fundo }, textColor: cores.texto, fontSize: 11 },
    grid: { vertLines: { color: cores.grade }, horzLines: { color: cores.grade } },
    rightPriceScale: { borderColor: cores.borda },
    timeScale: { borderColor: cores.borda, timeVisible: true, secondsVisible: false, tickMarkFormatter: marcaDoEixo },
    localization: {
      locale: "pt-BR",
      timeFormatter: (tempo) => diaHoraBrasilia(Number(tempo) * 1000),
      priceFormatter: formatarPreco,
    },
    crosshair: { mode: LC.CrosshairMode ? LC.CrosshairMode.Normal : 0 },
  });
  grafico.instancia = instancia;

  const formatoDaSerie = { type: "price", precision: casas, minMove: Math.pow(10, -casas) };
  const serieLong = instancia.addLineSeries({ color: cores.long, lineWidth: 2, priceLineVisible: false, priceFormat: formatoDaSerie });
  const serieShort = instancia.addLineSeries({ color: cores.short, lineWidth: 2, priceLineVisible: false, priceFormat: formatoDaSerie });
  serieLong.setData(grafico.pontos.filter((p) => numeroValido(p.precoLong)).map((p) => ({ time: p.tempo, value: p.precoLong })));
  serieShort.setData(grafico.pontos.filter((p) => numeroValido(p.precoShort)).map((p) => ({ time: p.tempo, value: p.precoShort })));
  instancia.timeScale().fitContent();
  instancia.subscribeCrosshairMove((parametro) => mostrarDica(parametro, cores, formatarPreco));
}

/** A caixinha que segue o mouse: horario, precos, lotes, spread e spread medio daquele ponto ate agora. */
function mostrarDica(parametro, cores, formatarPreco) {
  const dica = porId("grafico-dica");
  const ponto = parametro && parametro.point;
  const indice = parametro && parametro.time !== undefined ? grafico.indicePorTempo.get(Number(parametro.time)) : undefined;
  if (!ponto || ponto.x < 0 || ponto.y < 0 || indice === undefined) {
    dica.hidden = true;
    return;
  }
  const p = grafico.pontos[indice];
  const ultimo = grafico.pontos[grafico.pontos.length - 1];
  const long = rotuloPonta(grafico.sinal.ponta_long);
  const short = rotuloPonta(grafico.sinal.ponta_short);
  const linhas = [
    [null, `${long}: ${numeroValido(p.precoLong) ? formatarPreco(p.precoLong) : "—"}`, cores.long],
    [null, `${short}: ${numeroValido(p.precoShort) ? formatarPreco(p.precoShort) : "—"}`, cores.short],
    [null, `Lote ${long}: ${usdCompacto(p.loteLong)}`],
    [null, `Lote ${short}: ${usdCompacto(p.loteShort)}`],
    [null, `Spread: ${pct(p.spread)}`],
    [null, `Spread médio ${horaMinutoBrasilia(p.tempo * 1000)} – ${horaMinutoBrasilia(ultimo.tempo * 1000)}: ${pct(grafico.mediaAteOFim[indice])}`],
  ];
  dica.replaceChildren(criar("div", "dica-titulo", diaHoraBrasilia(p.tempo * 1000)));
  for (const [, texto, cor] of linhas) {
    const linha = criar("div");
    if (cor) {
      const quadrado = criar("span", "dica-cor");
      quadrado.style.background = cor;
      linha.appendChild(quadrado);
    }
    linha.appendChild(document.createTextNode(texto));
    dica.appendChild(linha);
  }
  dica.hidden = false;
  // Fica ao lado do mouse, sem sair da area do grafico.
  const area = dica.parentElement.getBoundingClientRect();
  const largura = dica.offsetWidth;
  const altura = dica.offsetHeight;
  let x = ponto.x + 16;
  let y = ponto.y + 16;
  if (x + largura > area.width) x = Math.max(0, ponto.x - largura - 16);
  if (y + altura > area.height) y = Math.max(0, ponto.y - altura - 16);
  dica.style.left = x + "px";
  dica.style.top = y + "px";
}

function prepararGrafico() {
  porId("grafico-fechar").addEventListener("click", fecharGrafico);
  porId("modal-grafico").addEventListener("click", (evento) => {
    if (evento.target === evento.currentTarget) fecharGrafico();  // clique fora da caixa
  });
  for (const botao of document.querySelectorAll("[data-intencao]")) {
    botao.addEventListener("click", () => {
      if (grafico.intencao === botao.dataset.intencao) return;
      grafico.intencao = botao.dataset.intencao;
      carregarGrafico();
    });
  }
}

// ============================================================================
// 12. Inicio
// ============================================================================

function iniciar() {
  prepararFormulario();
  prepararSessao();
  prepararGrupos();
  prepararTabela();
  prepararBarra();
  prepararGrafico();

  // Esc fecha o grafico e as listas abertas; clique fora fecha as listas.
  document.addEventListener("keydown", (evento) => {
    if (evento.key !== "Escape") return;
    fecharMenus();
    fecharGrafico();
  });
  document.addEventListener("click", fecharMenus);

  atualizarControlesDeSessao();
  desenharAgora();
  acompanharSaude();
  conectar();  // ao conectar, traz a sessao e os grupos (sincronizarComServidor)
}

iniciar();
