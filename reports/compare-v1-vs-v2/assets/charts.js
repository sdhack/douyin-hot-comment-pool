// assets/charts.js — 抖音爆款评论池 v1→v2 对比报告图表
(function () {
  var cs = getComputedStyle(document.documentElement);
  function v(name) { return cs.getPropertyValue(name).trim(); }
  var accent = v('--accent');
  var accent2 = v('--accent2');
  var ink = v('--ink');
  var muted = v('--muted');
  var rule = v('--rule');
  var baseBar = v('--base-bar');
  var bad = v('--bad');

  function fmtSec(s) {
    if (s >= 60) {
      var m = Math.floor(s / 60);
      var r = Math.round(s % 60);
      return m + ' 分 ' + r + ' 秒';
    }
    return (Math.round(s * 10) / 10) + ' 秒';
  }

  // ---- 图1 · 端到端耗时对比 ----
  var elRuntime = document.getElementById('chart-runtime');
  if (elRuntime && window.echarts) {
    var chartRuntime = echarts.init(elRuntime, null, { renderer: 'svg' });
    chartRuntime.setOption({
      animation: false,
      tooltip: {
        appendToBody: true,
        trigger: 'item',
        formatter: function (p) { return p.name + '<br/><b>' + fmtSec(p.value) + '</b>'; }
      },
      grid: { left: 150, right: 110, top: 12, bottom: 28 },
      xAxis: {
        type: 'value',
        axisLabel: { color: muted, formatter: function (s) { return Math.round(s / 60 * 10) / 10 + ' min'; } },
        splitLine: { lineStyle: { color: rule } }
      },
      yAxis: {
        type: 'category',
        inverse: true,
        data: ['v1 基线 · 单轮', 'v2 · 四轮累计', 'v2 首轮 · 全流程'],
        axisLabel: { color: ink, fontWeight: 600, fontSize: 13 },
        axisLine: { lineStyle: { color: rule } },
        axisTick: { show: false }
      },
      series: [{
        type: 'bar',
        barWidth: 30,
        data: [
          { value: 1530, itemStyle: { color: baseBar } },
          { value: 503, itemStyle: { color: accent } },
          { value: 81.8, itemStyle: { color: accent2 } }
        ],
        itemStyle: { borderRadius: [0, 6, 6, 0] },
        label: {
          show: true,
          position: 'right',
          color: ink,
          fontWeight: 700,
          formatter: function (p) { return fmtSec(p.value); }
        }
      }]
    });
    window.addEventListener('resize', function () { chartRuntime.resize(); });
  }

  // ---- 图3 · 首轮三层漏斗诊断 ----
  var elFunnel = document.getElementById('chart-funnel');
  if (elFunnel && window.echarts) {
    var chartFunnel = echarts.init(elFunnel, null, { renderer: 'svg' });
    chartFunnel.setOption({
      animation: false,
      tooltip: {
        appendToBody: true,
        trigger: 'item',
        formatter: function (p) { return p.name + '<br/><b>' + p.value + ' 条</b>'; }
      },
      grid: { left: 170, right: 70, top: 14, bottom: 28 },
      xAxis: {
        type: 'value',
        axisLabel: { color: muted },
        splitLine: { lineStyle: { color: rule } }
      },
      yAxis: {
        type: 'category',
        inverse: true,
        data: ['T0 读入评论', 'T1 互动达标(OR)', 'T2 长度≥30字', 'T3 评分≥55'],
        axisLabel: { color: ink, fontWeight: 600, fontSize: 13 },
        axisLine: { lineStyle: { color: rule } },
        axisTick: { show: false }
      },
      series: [{
        type: 'bar',
        barWidth: 28,
        data: [
          { value: 150, itemStyle: { color: accent } },
          { value: 46, itemStyle: { color: accent + 'cc' } },
          { value: 12, itemStyle: { color: accent + '99' } },
          { value: 0, itemStyle: { color: bad } }
        ],
        itemStyle: { borderRadius: [0, 6, 6, 0] },
        label: {
          show: true,
          position: 'right',
          color: ink,
          fontWeight: 700,
          formatter: function (p) {
            return p.value === 0 ? '0（颗粒无收）' : p.value + ' 条';
          }
        }
      }]
    });
    window.addEventListener('resize', function () { chartFunnel.resize(); });
  }

  // ---- Mermaid 架构图 ----
  if (window.mermaid) {
    mermaid.initialize({
      startOnLoad: true,
      theme: 'neutral',
      securityLevel: 'loose',
      flowchart: { htmlLabels: true, curve: 'basis' }
    });
  }
})();
