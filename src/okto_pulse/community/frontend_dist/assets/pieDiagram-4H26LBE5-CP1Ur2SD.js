import{D as e,O as t}from"./src-BKR-Ap-5.js";import{H as n,K as r,U as i,a,c as o,f as s,v as c,w as l,x as u,y as d}from"./chunk-CSCIHK7Q-DlaI-3Hx.js";import{n as f}from"./path-BWPyau1x.js";import{m as p}from"./dist-Beqw4fXJ.js";import{t as m}from"./arc-DA1FCpuC.js";import{t as h}from"./array-BifhSqXX.js";import{i as g,p as _}from"./chunk-5ZQYHXKU-D0E2hIGz.js";import{O as v,u as y}from"./index-dWwnuN5i.js";import{t as b}from"./mermaid-parser.core-C3RO1Szj.js";import{t as x}from"./chunk-4BX2VUAB-CT7Or1GW.js";function S(e,t){return t<e?-1:t>e?1:t>=e?0:NaN}function C(e){return e}function w(){var e=C,t=S,n=null,r=f(0),i=f(p),a=f(0);function o(o){var s,c=(o=h(o)).length,l,u,d=0,f=Array(c),m=Array(c),g=+r.apply(this,arguments),_=Math.min(p,Math.max(-p,i.apply(this,arguments)-g)),v,y=Math.min(Math.abs(_)/c,a.apply(this,arguments)),b=y*(_<0?-1:1),x;for(s=0;s<c;++s)(x=m[f[s]=s]=+e(o[s],s,o))>0&&(d+=x);for(t==null?n!=null&&f.sort(function(e,t){return n(o[e],o[t])}):f.sort(function(e,n){return t(m[e],m[n])}),s=0,u=d?(_-c*b)/d:0;s<c;++s,g=v)l=f[s],x=m[l],v=g+(x>0?x*u:0)+b,m[l]={data:o[l],index:s,value:x,startAngle:g,endAngle:v,padAngle:y};return m}return o.value=function(t){return arguments.length?(e=typeof t==`function`?t:f(+t),o):e},o.sortValues=function(e){return arguments.length?(t=e,n=null,o):t},o.sort=function(e){return arguments.length?(n=e,t=null,o):n},o.startAngle=function(e){return arguments.length?(r=typeof e==`function`?e:f(+e),o):r},o.endAngle=function(e){return arguments.length?(i=typeof e==`function`?e:f(+e),o):i},o.padAngle=function(e){return arguments.length?(a=typeof e==`function`?e:f(+e),o):a},o}var T=s.pie,E={sections:new Map,showData:!1,config:T},D=E.sections,O=E.showData,k=structuredClone(T),A={getConfig:e(()=>structuredClone(k),`getConfig`),clear:e(()=>{D=new Map,O=E.showData,a()},`clear`),setDiagramTitle:r,getDiagramTitle:l,setAccTitle:i,getAccTitle:d,setAccDescription:n,getAccDescription:c,addSection:e(({label:e,value:n})=>{if(n<0)throw Error(`"${e}" has invalid value: ${n}. Negative values are not allowed in pie charts. All slice values must be >= 0.`);D.has(e)||(D.set(e,n),t.debug(`added new section: ${e}, with value: ${n}`))},`addSection`),getSections:e(()=>D,`getSections`),setShowData:e(e=>{O=e},`setShowData`),getShowData:e(()=>O,`getShowData`)},j=e((e,t)=>{x(e,t),t.setShowData(e.showData),e.sections.map(t.addSection)},`populateDb`),M={parse:e(async e=>{let n=await b(`pie`,e);t.debug(n),j(n,A)},`parse`)},N=e(e=>`
  .pieCircle{
    stroke: ${e.pieStrokeColor};
    stroke-width : ${e.pieStrokeWidth};
    opacity : ${e.pieOpacity};
  }
  .pieOuterCircle{
    stroke: ${e.pieOuterStrokeColor};
    stroke-width: ${e.pieOuterStrokeWidth};
    fill: none;
  }
  .pieTitleText {
    text-anchor: middle;
    font-size: ${e.pieTitleTextSize};
    fill: ${e.pieTitleTextColor};
    font-family: ${e.fontFamily};
  }
  .slice {
    font-family: ${e.fontFamily};
    fill: ${e.pieSectionTextColor};
    font-size:${e.pieSectionTextSize};
    // fill: white;
  }
  .legend text {
    fill: ${e.pieLegendTextColor};
    font-family: ${e.fontFamily};
    font-size: ${e.pieLegendTextSize};
  }
`,`getStyles`),P=e(e=>{let t=[...e.values()].reduce((e,t)=>e+t,0),n=[...e.entries()].map(([e,t])=>({label:e,value:t})).filter(e=>e.value/t*100>=1);return w().value(e=>e.value).sort(null)(n)},`createPieArcs`),F={parser:M,db:A,renderer:{draw:e((e,n,r,i)=>{t.debug(`rendering pie chart
`+e);let a=i.db,s=u(),c=g(a.getConfig(),s.pie),l=y(n),d=l.append(`g`);d.attr(`transform`,`translate(225,225)`);let{themeVariables:f}=s,[p]=_(f.pieOuterStrokeWidth);p??=2;let h=c.textPosition,b=m().innerRadius(0).outerRadius(185),x=m().innerRadius(185*h).outerRadius(185*h);d.append(`circle`).attr(`cx`,0).attr(`cy`,0).attr(`r`,185+p/2).attr(`class`,`pieOuterCircle`);let S=a.getSections(),C=P(S),w=[f.pie1,f.pie2,f.pie3,f.pie4,f.pie5,f.pie6,f.pie7,f.pie8,f.pie9,f.pie10,f.pie11,f.pie12],T=0;S.forEach(e=>{T+=e});let E=C.filter(e=>(e.data.value/T*100).toFixed(0)!==`0`),D=v(w).domain([...S.keys()]);d.selectAll(`mySlices`).data(E).enter().append(`path`).attr(`d`,b).attr(`fill`,e=>D(e.data.label)).attr(`class`,`pieCircle`),d.selectAll(`mySlices`).data(E).enter().append(`text`).text(e=>(e.data.value/T*100).toFixed(0)+`%`).attr(`transform`,e=>`translate(`+x.centroid(e)+`)`).style(`text-anchor`,`middle`).attr(`class`,`slice`);let O=d.append(`text`).text(a.getDiagramTitle()).attr(`x`,0).attr(`y`,-400/2).attr(`class`,`pieTitleText`),k=[...S.entries()].map(([e,t])=>({label:e,value:t})),A=d.selectAll(`.legend`).data(k).enter().append(`g`).attr(`class`,`legend`).attr(`transform`,(e,t)=>{let n=22*k.length/2;return`translate(216,`+(t*22-n)+`)`});A.append(`rect`).attr(`width`,18).attr(`height`,18).style(`fill`,e=>D(e.label)).style(`stroke`,e=>D(e.label)),A.append(`text`).attr(`x`,22).attr(`y`,14).text(e=>a.getShowData()?`${e.label} [${e.value}]`:e.label);let j=512+Math.max(...A.selectAll(`text`).nodes().map(e=>e?.getBoundingClientRect().width??0)),M=O.node()?.getBoundingClientRect().width??0,N=450/2-M/2,F=450/2+M/2,I=Math.min(0,N),L=Math.max(j,F)-I;l.attr(`viewBox`,`${I} 0 ${L} 450`),o(l,450,L,c.useMaxWidth)},`draw`)},styles:N};export{F as diagram};