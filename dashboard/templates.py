"""
看板 HTML 模板渲染
每个板块一个函数，主函数 render_html() 组合输出完整页面。
"""


_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'SF Mono','Menlo','Microsoft YaHei',monospace;background:#0b0f19;color:#b0bec5;padding:8px;font-size:13px}
h2{font-size:14px;color:#00d4aa;margin:10px 0 6px 0;padding-bottom:4px;border-bottom:1px solid #1a2540}
.header{text-align:center;padding:8px 0;border-bottom:1px solid #1a2540;margin-bottom:8px}
.header .title{font-size:18px;color:#00e5ff;font-weight:bold}
.header .sub{font-size:11px;color:#5c6e80;margin-top:2px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px}
.dot-live{background:#00e676;animation:pulse 2s infinite}
.dot-off{background:#ff5252}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.3}}
.idx-row{display:flex;gap:8px;margin:8px 0}
.idx-item{flex:1;background:#111827;border-radius:6px;padding:8px;text-align:center}
.idx-item .name{font-size:10px;color:#5c6e80}.idx-item .val{font-size:16px;font-weight:bold}
.up{color:#ff5252}.down{color:#00e676}
.sent-bar{height:4px;background:#1a2540;border-radius:2px;margin:4px 0;overflow:hidden}
.sent-fill{height:100%;border-radius:2px;transition:width 0.5s}
.sent-sub{display:flex;gap:4px;font-size:10px;color:#5c6e80}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px}
.card{background:#111827;border-radius:6px;padding:10px;margin:6px 0}
.card-sm{background:#111827;border-radius:4px;padding:6px}
.card .label{font-size:10px;color:#5c6e80;text-transform:uppercase}
.card .value{font-size:15px;font-weight:bold}
.card .sub{font-size:10px;color:#5c6e80}
.tag{display:inline-block;font-size:10px;padding:1px 6px;border-radius:8px;margin:1px}
.tag-green{background:#0d3320;color:#00e676}
.tag-red{background:#330d15;color:#ff5252}
.tag-blue{background:#0d1f33;color:#40c4ff}
.tag-yellow{background:#332a0d;color:#ffd740}
.tbl{width:100%;border-collapse:collapse;font-size:11px}
.tbl th{color:#5c6e80;text-align:left;padding:3px 4px;font-weight:normal;border-bottom:1px solid #1a2540}
.tbl td{padding:4px;border-bottom:1px solid #0d1420}
.sector-scroll{display:flex;gap:6px;overflow-x:auto;padding:4px 0;-webkit-overflow-scrolling:touch}
.sector-chip{flex-shrink:0;background:#111827;border-radius:6px;padding:6px 10px;min-width:90px;text-align:center}
.sector-chip .n{font-size:14px;font-weight:bold}
.sector-chip .s{font-size:10px;color:#5c6e80}
.eq-bar{display:inline-block;width:3px;margin:0 1px;border-radius:1px;vertical-align:middle}
.footer{text-align:center;color:#2a3a50;font-size:10px;margin-top:12px;padding:8px 0}
.footer a{color:#2a3a50}
"""


# ============================================================================
# JavaScript
# ============================================================================

_JS_HEADER = """
function S(v){return v>0?'+'+v:''+v}
function P(v){return (v>0?'<span class="up">+'+v+'%</span>':v<0?'<span class="down">'+v+'%</span>':'0%')}
function B(v,cls){return '<span class="tag '+(cls||'tag-blue')+'">'+v+'</span>'}

async function load(){
try{const r=await fetch('/dashboard');const d=(await r.json()).data;
const dot=document.getElementById('statusDot');
dot.className='dot '+(d.updated?'dot-live':'dot-off');
document.getElementById('headerSub').textContent=
  (d.trading_day?'🟢 交易日':'⚫ 非交易日')+' | 更新 '+d.timestamp+(d.ai_status.monitor_running?' | 监控运行中':'');
let html='';
"""

def _section_index():
    return """
// ---- 大盘指数 ----
if(d.index && d.index.sh_close){
  html+='<div class="idx-row">';
  html+='<div class="idx-item"><div class="name">上证</div><div class="val">'+(d.index.sh_close||0).toFixed(0)+'</div><div>'+P(d.index.sh_change_pct)+'</div></div>';
  html+='<div class="idx-item"><div class="name">深证</div><div class="val">'+P(d.index.sz_change_pct)+'</div></div>';
  html+='<div class="idx-item"><div class="name">创业板</div><div class="val">'+P(d.index.gem_change_pct)+'</div></div>';
  html+='<div class="idx-item"><div class="name">成交额</div><div class="val" style="font-size:12px">'+(d.index.total_amount||'')+'</div></div>';
  html+='</div>';
}
"""



def _section_style():
    return """
// ---- 市场风格 + 情绪 ----
var sc=parseInt(d.emotion.sentiment_index)||0;
var scColor=sc>=70?'#ff5252':sc>=40?'#ffd740':'#00e676';
html+='<div class="card">';
html+='<div class="label">市场风格</div>';
html+='<div style="font-size:18px;font-weight:bold;color:#00e5ff">'+d.market.style+(d.market.cycle_stage?' → '+d.market.cycle_stage:'')+'</div>';
html+='<div class="sub">'+d.market.strategy+(d.market.reason?' | '+d.market.reason:'')+'</div>';
html+='<div style="margin-top:6px"><span class="label">情绪分</span> <span style="font-size:22px;font-weight:bold;color:'+scColor+'">'+sc+'</span><span style="font-size:10px;color:#5c6e80">/100</span></div>';
html+='<div class="sent-bar"><div class="sent-fill" style="width:'+sc+'%;background:'+scColor+'"></div></div>';
html+='<div class="sent-sub"><span>溢价'+d.emotion.score_premium+'</span><span>宽度'+d.emotion.score_breadth+'</span><span>高度'+d.emotion.score_height+'</span><span>承接'+d.emotion.score_support+'</span><span>红盘'+d.emotion.red_rate+'</span></div>';
html+='</div>';
"""



def _section_breadth():
    return """
// ---- 涨跌分布 ----
html+='<div class="grid3">';
html+='<div class="card-sm" style="text-align:center"><div class="label">涨停</div><div style="font-size:20px;color:#ff5252;font-weight:bold">'+d.breadth.zt_count+'</div><div class="sub">'+d.breadth.zt_source+'</div></div>';
html+='<div class="card-sm" style="text-align:center"><div class="label">跌停</div><div style="font-size:20px;color:#00e676;font-weight:bold">'+d.breadth.dt_count+'</div></div>';
html+='<div class="card-sm" style="text-align:center"><div class="label">炸板</div><div style="font-size:20px;color:#ffd740;font-weight:bold">'+d.breadth.zhaban_count+'</div><div class="sub">'+d.breadth.zhaban_rate+'</div></div>';
html+='<div class="card-sm" style="text-align:center"><div class="label">最高连板</div><div style="font-size:20px;color:#40c4ff;font-weight:bold">'+d.breadth.height+'板</div></div>';
html+='<div class="card-sm" style="text-align:center"><div class="label">上涨</div><div style="font-size:20px;color:#ff5252;font-weight:bold">'+d.breadth.up_count+'</div></div>';
html+='<div class="card-sm" style="text-align:center"><div class="label">下跌</div><div style="font-size:20px;color:#00e676;font-weight:bold">'+d.breadth.down_count+'</div></div>';
html+='</div>';
"""



def _section_portfolio():
    return """
// ---- 持仓盈亏 ----
html+='<h2>📋 持仓 ('+d.portfolio.active+'只 | AI '+d.portfolio.ai_auto+' | 手动 '+d.portfolio.manual+')</h2>';
html+='<div class="grid3">';
html+='<div class="card-sm" style="text-align:center"><div class="label">今日盈亏</div><div style="font-size:16px;font-weight:bold">'+P(d.portfolio.today_pnl_pct)+'</div><div>'+S(d.portfolio.today_pnl)+'元</div></div>';
html+='<div class="card-sm" style="text-align:center"><div class="label">累计盈亏</div><div style="font-size:16px;font-weight:bold">'+P(d.portfolio.cumulative_pnl_pct)+'</div><div>'+S(d.portfolio.cumulative_pnl)+'元</div></div>';
html+='<div class="card-sm" style="text-align:center"><div class="label">盈/亏</div><div style="font-size:16px;font-weight:bold"><span class="up">'+d.portfolio.profit_count+'</span>/<span class="down">'+d.portfolio.loss_count+'</span></div></div>';
html+='</div>';
if(d.portfolio.positions && d.portfolio.positions.length>0){
  html+='<table class="tbl"><tr><th>股票</th><th>盈亏</th><th>今日</th><th>策略</th></tr>';
  d.portfolio.positions.forEach(function(p){
    var pe=p.profit_pct>0?'up':'down';
    html+='<tr><td>'+p.name+'<br><span style="font-size:10px;color:#5c6e80">'+p.code+'</span></td>';
    html+='<td class="'+pe+'" style="font-weight:bold">'+S(p.profit_pct)+'%</td>';
    html+='<td>'+P(p.today_change)+'</td>';
    html+='<td><span class="tag '+(p.type==='AI_AUTO'?'tag-green':'tag-blue')+'">'+(p.strategy||p.type)+'</span></td></tr>';
  });
  html+='</table>';
} else {
  html+='<div class="card-sm" style="text-align:center;color:#5c6e80">暂无持仓</div>';
}
"""



def _section_sectors():
    return """
// ---- 热门板块 ----
if(d.sectors && d.sectors.length>0){
  var sd=d.sectors_date||'';
  html+='<h2>🔥 热门板块 <span style="font-size:10px;color:#5c6e80;font-weight:normal">'+sd+'</span></h2><div class="sector-scroll">';
  d.sectors.forEach(function(s){
    var a='';if(s.accel>0)a='<span class="up">+'+s.accel+'</span>';else if(s.accel<0)a='<span class="down">'+s.accel+'</span>';
    html+='<div class="sector-chip"><div class="n">'+s.zt_count+'只</div><div style="font-size:12px;font-weight:bold">'+s.sector+'</div><div class="s">'+a+'</div></div>';
  });
  html+='</div>';
}
"""



def _section_dragons():
    return """
// ---- 涨停龙头 ----
if(d.dragons && d.dragons.length>0){
  html+='<h2>🐉 涨停龙头 <span style="font-size:10px;color:#5c6e80;font-weight:normal">'+(d.dragons_date||'')+'</span></h2><table class="tbl"><tr><th>名称</th><th>连板</th><th>板块</th></tr>';
  d.dragons.forEach(function(g){
    html+='<tr><td>'+g.name+'<br><span style="font-size:10px;color:#5c6e80">'+g.code+'</span></td>';
    html+='<td style="font-size:16px;font-weight:bold;color:#ffd740">'+g.lbc+'板</td>';
    html+='<td style="font-size:10px">'+g.industry+'</td></tr>';
  });
  html+='</table>';
}
"""



def _section_equity():
    return """
// ---- 净值曲线 ----
if(d.equity_curve && d.equity_curve.length>3){
  html+='<h2>📈 净值曲线 <span style="font-size:10px;color:#5c6e80;font-weight:normal">'+(d.equity_date||'')+' 近'+d.equity_curve.length+'日</span></h2>';
  var eqs=d.equity_curve;
  var minE=Math.min.apply(null,eqs.map(function(e){return e.equity}));
  var maxE=Math.max.apply(null,eqs.map(function(e){return e.equity}));
  var range=maxE-minE||1;
  html+='<div class="card"><div style="height:60px;display:flex;align-items:flex-end;gap:2px">';
  eqs.forEach(function(e){
    var h=((e.equity-minE)/range*100).toFixed(0);
    var c=e.pnl_pct>0?'#ff5252':'#00e676';
    html+='<div class="eq-bar" style="height:'+h+'%;background:'+c+'" title="'+e.date+': '+S(e.pnl_pct)+'%"></div>';
  });
  html+='</div>';
  html+='<div class="grid2" style="margin-top:4px"><div class="sub">起始: '+eqs[0].date+'</div>';
  html+='<div class="sub" style="text-align:right">最新: '+eqs[eqs.length-1].date+' 权益'+eqs[eqs.length-1].equity.toFixed(0)+'</div></div>';
  html+='</div>';
}
"""



def _section_ai_status():
    return """
// ---- AI 状态 ----
html+='<h2>⚙️ 系统状态</h2>';
html+='<div class="card"><div class="grid3">';
html+='<div class="card-sm" style="text-align:center"><div class="label">监控引擎</div><div style="font-weight:bold;color:'+(d.ai_status.monitor_running?'#00e676':'#ff5252')+'">'+(d.ai_status.monitor_running?'运行中':'已停止')+'</div><div class="sub">'+d.ai_status.last_cycle+'</div></div>';
html+='<div class="card-sm" style="text-align:center"><div class="label">持仓熔断</div><div style="font-weight:bold;color:'+(d.ai_status.circuit_breaker?'#ff5252;font-size:14px':'#5c6e80')+'">'+(d.ai_status.circuit_breaker?'⚠ 已触发':'正常')+'</div></div>';
html+='<div class="card-sm" style="text-align:center"><div class="label">大盘熔断</div><div style="font-weight:bold;color:'+(d.ai_status.index_breaker?'#ff5252;font-size:14px':'#5c6e80')+'">'+(d.ai_status.index_breaker?'⚠ 已触发':'正常')+'</div></div>';
html+='<div class="card-sm" style="text-align:center"><div class="label">最大持仓</div><div style="font-weight:bold">'+d.ai_status.max_positions+'只</div></div>';
html+='<div class="card-sm" style="text-align:center"><div class="label">每日买入上限</div><div style="font-weight:bold">'+d.ai_status.max_daily_buys+'笔</div></div>';
html+='<div class="card-sm" style="text-align:center"><div class="label">数据时间</div><div style="font-weight:bold;color:#00d4aa">'+d.timestamp+'</div></div>';
html+='</div></div>';
"""



def _section_jobs():
    return """
// ---- 定时任务 ----
if(d.jobs && d.jobs.length>0){
  html+='<h2>⏰ 定时任务</h2>';
  html+='<table class="tbl"><tr><th>时间</th><th>任务</th><th>今日</th><th>最后执行</th></tr>';
  d.jobs.forEach(function(j){
    var ran=j.ran_today?'<span class="tag tag-green">已执行</span>':'<span class="tag tag-red">未执行</span>';
    html+='<tr><td style="font-weight:bold;color:#40c4ff">'+j.time+'</td>';
    html+='<td><div>'+j.name+'</div><div style="font-size:10px;color:#5c6e80">'+j.desc+'</div></td>';
    html+='<td>'+ran+'</td>';
    html+='<td style="font-size:10px;color:#5c6e80">'+j.last_run+'</td></tr>';
  });
  html+='</table>';
}
"""


_JS_SECTIONS = (
    _section_index()
    + _section_style()
    + _section_breadth()
    + _section_portfolio()
    + _section_sectors()
    + _section_dragons()
    + _section_equity()
    + _section_ai_status()
    + _section_jobs()
)

_JS_FOOTER = """
document.getElementById('app').innerHTML=html;
document.getElementById('refreshInfo').textContent='刷新 '+d.timestamp+' | 手动刷新页面更新';
}catch(e){document.getElementById('app').innerHTML='<div class="card" style="text-align:center;color:#ff5252">加载失败: '+e.message+'</div>';}
}
load();
"""


# ============================================================================
# 板块渲染函数（每个返回一段 JS 字符串）
# ============================================================================

_HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DragonPulse 系统看板</title>
<style>
""" + _CSS + """</style></head>
<body>
<div class="header">
  <div class="title"><span class="dot" id="statusDot"></span> DragonPulse</div>
  <div class="sub" id="headerSub">加载中...</div>
</div>
<div id="app">加载中...</div>
<div class="footer"><span id="refreshInfo"></span> | <a href="/docs">API</a> | <a href="/dashboard">JSON</a></div>
<script>
""" + _JS_HEADER + _JS_SECTIONS + _JS_FOOTER + """</script></body></html>"""


# ============================================================================
# CSS
# ============================================================================

def render_html() -> str:
    """渲染完整看板 HTML 页面"""
    return _HTML_PAGE


# ============================================================================
# 主页面骨架
# ============================================================================

_HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DragonPulse 系统看板</title>
<style>
""" + _CSS + """</style></head>
<body>
<div class="header">
  <div class="title"><span class="dot" id="statusDot"></span> DragonPulse</div>
  <div class="sub" id="headerSub">加载中...</div>
</div>
<div id="app">加载中...</div>
<div class="footer"><span id="refreshInfo"></span> | <a href="/docs">API</a> | <a href="/dashboard">JSON</a></div>
<script>
""" + _JS_HEADER + _JS_SECTIONS + _JS_FOOTER + """</script></body></html>"""


# ============================================================================
# CSS
# ============================================================================

_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'SF Mono','Menlo','Microsoft YaHei',monospace;background:#0b0f19;color:#b0bec5;padding:8px;font-size:13px}
h2{font-size:14px;color:#00d4aa;margin:10px 0 6px 0;padding-bottom:4px;border-bottom:1px solid #1a2540}
.header{text-align:center;padding:8px 0;border-bottom:1px solid #1a2540;margin-bottom:8px}
.header .title{font-size:18px;color:#00e5ff;font-weight:bold}
.header .sub{font-size:11px;color:#5c6e80;margin-top:2px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px}
.dot-live{background:#00e676;animation:pulse 2s infinite}
.dot-off{background:#ff5252}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.3}}
.idx-row{display:flex;gap:8px;margin:8px 0}
.idx-item{flex:1;background:#111827;border-radius:6px;padding:8px;text-align:center}
.idx-item .name{font-size:10px;color:#5c6e80}.idx-item .val{font-size:16px;font-weight:bold}
.up{color:#ff5252}.down{color:#00e676}
.sent-bar{height:4px;background:#1a2540;border-radius:2px;margin:4px 0;overflow:hidden}
.sent-fill{height:100%;border-radius:2px;transition:width 0.5s}
.sent-sub{display:flex;gap:4px;font-size:10px;color:#5c6e80}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px}
.card{background:#111827;border-radius:6px;padding:10px;margin:6px 0}
.card-sm{background:#111827;border-radius:4px;padding:6px}
.card .label{font-size:10px;color:#5c6e80;text-transform:uppercase}
.card .value{font-size:15px;font-weight:bold}
.card .sub{font-size:10px;color:#5c6e80}
.tag{display:inline-block;font-size:10px;padding:1px 6px;border-radius:8px;margin:1px}
.tag-green{background:#0d3320;color:#00e676}
.tag-red{background:#330d15;color:#ff5252}
.tag-blue{background:#0d1f33;color:#40c4ff}
.tag-yellow{background:#332a0d;color:#ffd740}
.tbl{width:100%;border-collapse:collapse;font-size:11px}
.tbl th{color:#5c6e80;text-align:left;padding:3px 4px;font-weight:normal;border-bottom:1px solid #1a2540}
.tbl td{padding:4px;border-bottom:1px solid #0d1420}
.sector-scroll{display:flex;gap:6px;overflow-x:auto;padding:4px 0;-webkit-overflow-scrolling:touch}
.sector-chip{flex-shrink:0;background:#111827;border-radius:6px;padding:6px 10px;min-width:90px;text-align:center}
.sector-chip .n{font-size:14px;font-weight:bold}
.sector-chip .s{font-size:10px;color:#5c6e80}
.eq-bar{display:inline-block;width:3px;margin:0 1px;border-radius:1px;vertical-align:middle}
.footer{text-align:center;color:#2a3a50;font-size:10px;margin-top:12px;padding:8px 0}
.footer a{color:#2a3a50}
"""


# ============================================================================
# JavaScript
# ============================================================================

_JS_HEADER = """
function S(v){return v>0?'+'+v:''+v}
function P(v){return (v>0?'<span class="up">+'+v+'%</span>':v<0?'<span class="down">'+v+'%</span>':'0%')}
function B(v,cls){return '<span class="tag '+(cls||'tag-blue')+'">'+v+'</span>'}

async function load(){
try{const r=await fetch('/dashboard');const d=(await r.json()).data;
const dot=document.getElementById('statusDot');
dot.className='dot '+(d.updated?'dot-live':'dot-off');
document.getElementById('headerSub').textContent=
  (d.trading_day?'🟢 交易日':'⚫ 非交易日')+' | 更新 '+d.timestamp+(d.ai_status.monitor_running?' | 监控运行中':'');
let html='';
"""

_JS_SECTIONS = (
    _section_index()
    + _section_style()
    + _section_breadth()
    + _section_portfolio()
    + _section_sectors()
    + _section_dragons()
    + _section_equity()
    + _section_ai_status()
    + _section_jobs()
)

_JS_FOOTER = """
document.getElementById('app').innerHTML=html;
document.getElementById('refreshInfo').textContent='刷新 '+d.timestamp+' | 手动刷新页面更新';
}catch(e){document.getElementById('app').innerHTML='<div class="card" style="text-align:center;color:#ff5252">加载失败: '+e.message+'</div>';}
}
load();
"""


# ============================================================================
# 板块渲染函数（每个返回一段 JS 字符串）
# ============================================================================

