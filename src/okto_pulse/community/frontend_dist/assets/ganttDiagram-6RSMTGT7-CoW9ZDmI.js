import{A as e,D as t,N as n,O as r,c as i,d as a,f as o,g as s,h as c,j as l,l as u,m as d,w as f}from"./src-BKR-Ap-5.js";import{H as p,K as m,U as h,a as g,c as _,s as v,v as y,w as b,x,y as S}from"./chunk-CSCIHK7Q-DlaI-3Hx.js";import{t as C}from"./dist-Beqw4fXJ.js";import{g as w}from"./chunk-5ZQYHXKU-D0E2hIGz.js";import{A as T,C as E,E as D,S as O,T as k,_ as A,b as j,d as ee,f as te,g as ne,h as re,j as ie,m as ae,p as oe,v as se,w as ce,x as le,y as ue}from"./index-7TAxq821.js";function de(e){return e}var M=1,fe=2,pe=3,N=4,me=1e-6;function he(e){return`translate(`+e+`,0)`}function ge(e){return`translate(0,`+e+`)`}function _e(e){return t=>+e(t)}function ve(e,t){return t=Math.max(0,e.bandwidth()-t*2)/2,e.round()&&(t=Math.round(t)),n=>+e(n)+t}function ye(){return!this.__axis}function be(e,t){var n=[],r=null,i=null,a=6,o=6,s=3,c=typeof window<`u`&&window.devicePixelRatio>1?0:.5,l=e===M||e===N?-1:1,u=e===N||e===fe?`x`:`y`,d=e===M||e===pe?he:ge;function f(f){var p=r??(t.ticks?t.ticks.apply(t,n):t.domain()),m=i??(t.tickFormat?t.tickFormat.apply(t,n):de),h=Math.max(a,0)+s,g=t.range(),_=+g[0]+c,v=+g[g.length-1]+c,y=(t.bandwidth?ve:_e)(t.copy(),c),b=f.selection?f.selection():f,x=b.selectAll(`.domain`).data([null]),S=b.selectAll(`.tick`).data(p,t).order(),C=S.exit(),w=S.enter().append(`g`).attr(`class`,`tick`),T=S.select(`line`),E=S.select(`text`);x=x.merge(x.enter().insert(`path`,`.tick`).attr(`class`,`domain`).attr(`stroke`,`currentColor`)),S=S.merge(w),T=T.merge(w.append(`line`).attr(`stroke`,`currentColor`).attr(u+`2`,l*a)),E=E.merge(w.append(`text`).attr(`fill`,`currentColor`).attr(u,l*h).attr(`dy`,e===M?`0em`:e===pe?`0.71em`:`0.32em`)),f!==b&&(x=x.transition(f),S=S.transition(f),T=T.transition(f),E=E.transition(f),C=C.transition(f).attr(`opacity`,me).attr(`transform`,function(e){return isFinite(e=y(e))?d(e+c):this.getAttribute(`transform`)}),w.attr(`opacity`,me).attr(`transform`,function(e){var t=this.parentNode.__axis;return d((t&&isFinite(t=t(e))?t:y(e))+c)})),C.remove(),x.attr(`d`,e===N||e===fe?o?`M`+l*o+`,`+_+`H`+c+`V`+v+`H`+l*o:`M`+c+`,`+_+`V`+v:o?`M`+_+`,`+l*o+`V`+c+`H`+v+`V`+l*o:`M`+_+`,`+c+`H`+v),S.attr(`opacity`,1).attr(`transform`,function(e){return d(y(e)+c)}),T.attr(u+`2`,l*a),E.attr(u,l*h).text(m),b.filter(ye).attr(`fill`,`none`).attr(`font-size`,10).attr(`font-family`,`sans-serif`).attr(`text-anchor`,e===fe?`start`:e===N?`end`:`middle`),b.each(function(){this.__axis=y})}return f.scale=function(e){return arguments.length?(t=e,f):t},f.ticks=function(){return n=Array.from(arguments),f},f.tickArguments=function(e){return arguments.length?(n=e==null?[]:Array.from(e),f):n.slice()},f.tickValues=function(e){return arguments.length?(r=e==null?null:Array.from(e),f):r&&r.slice()},f.tickFormat=function(e){return arguments.length?(i=e,f):i},f.tickSize=function(e){return arguments.length?(a=o=+e,f):a},f.tickSizeInner=function(e){return arguments.length?(a=+e,f):a},f.tickSizeOuter=function(e){return arguments.length?(o=+e,f):o},f.tickPadding=function(e){return arguments.length?(s=+e,f):s},f.offset=function(e){return arguments.length?(c=+e,f):c},f}function xe(e){return be(M,e)}function Se(e){return be(pe,e)}var Ce=Math.PI/180,we=180/Math.PI,P=18,Te=.96422,Ee=1,De=.82521,Oe=4/29,F=6/29,ke=3*F*F,Ae=F*F*F;function je(e){if(e instanceof I)return new I(e.l,e.a,e.b,e.opacity);if(e instanceof L)return ze(e);e instanceof o||(e=d(e));var t=Ie(e.r),n=Ie(e.g),r=Ie(e.b),i=Ne((.2225045*t+.7168786*n+.0606169*r)/Ee),a,s;return t===n&&n===r?a=s=i:(a=Ne((.4360747*t+.3850649*n+.1430804*r)/Te),s=Ne((.0139322*t+.0971045*n+.7141733*r)/De)),new I(116*i-16,500*(a-i),200*(i-s),e.opacity)}function Me(e,t,n,r){return arguments.length===1?je(e):new I(e,t,n,r??1)}function I(e,t,n,r){this.l=+e,this.a=+t,this.b=+n,this.opacity=+r}c(I,Me,s(a,{brighter(e){return new I(this.l+P*(e??1),this.a,this.b,this.opacity)},darker(e){return new I(this.l-P*(e??1),this.a,this.b,this.opacity)},rgb(){var e=(this.l+16)/116,t=isNaN(this.a)?e:e+this.a/500,n=isNaN(this.b)?e:e-this.b/200;return t=Te*Pe(t),e=Ee*Pe(e),n=De*Pe(n),new o(Fe(3.1338561*t-1.6168667*e-.4906146*n),Fe(-.9787684*t+1.9161415*e+.033454*n),Fe(.0719453*t-.2289914*e+1.4052427*n),this.opacity)}}));function Ne(e){return e>Ae?e**(1/3):e/ke+Oe}function Pe(e){return e>F?e*e*e:ke*(e-Oe)}function Fe(e){return 255*(e<=.0031308?12.92*e:1.055*e**(1/2.4)-.055)}function Ie(e){return(e/=255)<=.04045?e/12.92:((e+.055)/1.055)**2.4}function Le(e){if(e instanceof L)return new L(e.h,e.c,e.l,e.opacity);if(e instanceof I||(e=je(e)),e.a===0&&e.b===0)return new L(NaN,0<e.l&&e.l<100?0:NaN,e.l,e.opacity);var t=Math.atan2(e.b,e.a)*we;return new L(t<0?t+360:t,Math.sqrt(e.a*e.a+e.b*e.b),e.l,e.opacity)}function Re(e,t,n,r){return arguments.length===1?Le(e):new L(e,t,n,r??1)}function L(e,t,n,r){this.h=+e,this.c=+t,this.l=+n,this.opacity=+r}function ze(e){if(isNaN(e.h))return new I(e.l,0,0,e.opacity);var t=e.h*Ce;return new I(e.l,Math.cos(t)*e.c,Math.sin(t)*e.c,e.opacity)}c(L,Re,s(a,{brighter(e){return new L(this.h,this.c,this.l+P*(e??1),this.opacity)},darker(e){return new L(this.h,this.c,this.l-P*(e??1),this.opacity)},rgb(){return ze(this).rgb()}}));function Be(e){return function(t,n){var r=e((t=Re(t)).h,(n=Re(n)).h),i=u(t.c,n.c),a=u(t.l,n.l),o=u(t.opacity,n.opacity);return function(e){return t.h=r(e),t.c=i(e),t.l=a(e),t.opacity=o(e),t+``}}}var Ve=Be(i),He=l(((e,t)=>{(function(n,r){typeof e==`object`&&t!==void 0?t.exports=r():typeof define==`function`&&define.amd?define(r):(n=typeof globalThis<`u`?globalThis:n||self).dayjs_plugin_isoWeek=r()})(e,(function(){var e=`day`;return function(t,n,r){var i=function(t){return t.add(4-t.isoWeekday(),e)},a=n.prototype;a.isoWeekYear=function(){return i(this).year()},a.isoWeek=function(t){if(!this.$utils().u(t))return this.add(7*(t-this.isoWeek()),e);var n,a,o,s,c=i(this),l=(n=this.isoWeekYear(),a=this.$u,o=(a?r.utc:r)().year(n).startOf(`year`),s=4-o.isoWeekday(),o.isoWeekday()>4&&(s+=7),o.add(s,e));return c.diff(l,`week`)+1},a.isoWeekday=function(e){return this.$utils().u(e)?this.day()||7:this.day(this.day()%7?e:e-7)};var o=a.startOf;a.startOf=function(e,t){var n=this.$utils(),r=!!n.u(t)||t;return n.p(e)===`isoweek`?r?this.date(this.date()-(this.isoWeekday()-1)).startOf(`day`):this.date(this.date()-1-(this.isoWeekday()-1)+7).endOf(`day`):o.bind(this)(e,t)}}}))})),Ue=l(((e,t)=>{(function(n,r){typeof e==`object`&&t!==void 0?t.exports=r():typeof define==`function`&&define.amd?define(r):(n=typeof globalThis<`u`?globalThis:n||self).dayjs_plugin_customParseFormat=r()})(e,(function(){var e={LTS:`h:mm:ss A`,LT:`h:mm A`,L:`MM/DD/YYYY`,LL:`MMMM D, YYYY`,LLL:`MMMM D, YYYY h:mm A`,LLLL:`dddd, MMMM D, YYYY h:mm A`},t=/(\[[^[]*\])|([-_:/.,()\s]+)|(A|a|Q|YYYY|YY?|ww?|MM?M?M?|Do|DD?|hh?|HH?|mm?|ss?|S{1,3}|z|ZZ?)/g,n=/\d/,r=/\d\d/,i=/\d\d?/,a=/\d*[^-_:/,()\s\d]+/,o={},s=function(e){return(e=+e)+(e>68?1900:2e3)},c=function(e){return function(t){this[e]=+t}},l=[/[+-]\d\d:?(\d\d)?|Z/,function(e){(this.zone||={}).offset=function(e){if(!e||e===`Z`)return 0;var t=e.match(/([+-]|\d\d)/g),n=60*t[1]+(+t[2]||0);return n===0?0:t[0]===`+`?-n:n}(e)}],u=function(e){var t=o[e];return t&&(t.indexOf?t:t.s.concat(t.f))},d=function(e,t){var n,r=o.meridiem;if(r){for(var i=1;i<=24;i+=1)if(e.indexOf(r(i,0,t))>-1){n=i>12;break}}else n=e===(t?`pm`:`PM`);return n},f={A:[a,function(e){this.afternoon=d(e,!1)}],a:[a,function(e){this.afternoon=d(e,!0)}],Q:[n,function(e){this.month=3*(e-1)+1}],S:[n,function(e){this.milliseconds=100*e}],SS:[r,function(e){this.milliseconds=10*e}],SSS:[/\d{3}/,function(e){this.milliseconds=+e}],s:[i,c(`seconds`)],ss:[i,c(`seconds`)],m:[i,c(`minutes`)],mm:[i,c(`minutes`)],H:[i,c(`hours`)],h:[i,c(`hours`)],HH:[i,c(`hours`)],hh:[i,c(`hours`)],D:[i,c(`day`)],DD:[r,c(`day`)],Do:[a,function(e){var t=o.ordinal,n=e.match(/\d+/);if(this.day=n[0],t)for(var r=1;r<=31;r+=1)t(r).replace(/\[|\]/g,``)===e&&(this.day=r)}],w:[i,c(`week`)],ww:[r,c(`week`)],M:[i,c(`month`)],MM:[r,c(`month`)],MMM:[a,function(e){var t=u(`months`),n=(u(`monthsShort`)||t.map((function(e){return e.slice(0,3)}))).indexOf(e)+1;if(n<1)throw Error();this.month=n%12||n}],MMMM:[a,function(e){var t=u(`months`).indexOf(e)+1;if(t<1)throw Error();this.month=t%12||t}],Y:[/[+-]?\d+/,c(`year`)],YY:[r,function(e){this.year=s(e)}],YYYY:[/\d{4}/,c(`year`)],Z:l,ZZ:l};function p(n){for(var r=n,i=o&&o.formats,a=(n=r.replace(/(\[[^\]]+])|(LTS?|l{1,4}|L{1,4})/g,(function(t,n,r){var a=r&&r.toUpperCase();return n||i[r]||e[r]||i[a].replace(/(\[[^\]]+])|(MMMM|MM|DD|dddd)/g,(function(e,t,n){return t||n.slice(1)}))}))).match(t),s=a.length,c=0;c<s;c+=1){var l=a[c],u=f[l],d=u&&u[0],p=u&&u[1];a[c]=p?{regex:d,parser:p}:l.replace(/^\[|\]$/g,``)}return function(e){for(var t={},n=0,r=0;n<s;n+=1){var i=a[n];if(typeof i==`string`)r+=i.length;else{var o=i.regex,c=i.parser,l=e.slice(r),u=o.exec(l)[0];c.call(t,u),e=e.replace(u,``)}}return function(e){var t=e.afternoon;if(t!==void 0){var n=e.hours;t?n<12&&(e.hours+=12):n===12&&(e.hours=0),delete e.afternoon}}(t),t}}return function(e,t,n){n.p.customParseFormat=!0,e&&e.parseTwoDigitYear&&(s=e.parseTwoDigitYear);var r=t.prototype,i=r.parse;r.parse=function(e){var t=e.date,r=e.utc,a=e.args;this.$u=r;var s=a[1];if(typeof s==`string`){var c=!0===a[2],l=!0===a[3],u=c||l,d=a[2];l&&(d=a[2]),o=this.$locale(),!c&&d&&(o=n.Ls[d]),this.$d=function(e,t,n,r){try{if([`x`,`X`].indexOf(t)>-1)return new Date((t===`X`?1e3:1)*e);var i=p(t)(e),a=i.year,o=i.month,s=i.day,c=i.hours,l=i.minutes,u=i.seconds,d=i.milliseconds,f=i.zone,m=i.week,h=new Date,g=s||(a||o?1:h.getDate()),_=a||h.getFullYear(),v=0;a&&!o||(v=o>0?o-1:h.getMonth());var y,b=c||0,x=l||0,S=u||0,C=d||0;return f?new Date(Date.UTC(_,v,g,b,x,S,C+60*f.offset*1e3)):n?new Date(Date.UTC(_,v,g,b,x,S,C)):(y=new Date(_,v,g,b,x,S,C),m&&(y=r(y).week(m).toDate()),y)}catch{return new Date(``)}}(t,s,r,n),this.init(),d&&!0!==d&&(this.$L=this.locale(d).$L),u&&t!=this.format(s)&&(this.$d=new Date(``)),o={}}else if(s instanceof Array)for(var f=s.length,m=1;m<=f;m+=1){a[1]=s[m-1];var h=n.apply(this,a);if(h.isValid()){this.$d=h.$d,this.$L=h.$L,this.init();break}m===f&&(this.$d=new Date(``))}else i.call(this,e)}}}))})),We=l(((e,t)=>{(function(n,r){typeof e==`object`&&t!==void 0?t.exports=r():typeof define==`function`&&define.amd?define(r):(n=typeof globalThis<`u`?globalThis:n||self).dayjs_plugin_advancedFormat=r()})(e,(function(){return function(e,t){var n=t.prototype,r=n.format;n.format=function(e){var t=this,n=this.$locale();if(!this.isValid())return r.bind(this)(e);var i=this.$utils(),a=(e||`YYYY-MM-DDTHH:mm:ssZ`).replace(/\[([^\]]+)]|Q|wo|ww|w|WW|W|zzz|z|gggg|GGGG|Do|X|x|k{1,2}|S/g,(function(e){switch(e){case`Q`:return Math.ceil((t.$M+1)/3);case`Do`:return n.ordinal(t.$D);case`gggg`:return t.weekYear();case`GGGG`:return t.isoWeekYear();case`wo`:return n.ordinal(t.week(),`W`);case`w`:case`ww`:return i.s(t.week(),e===`w`?1:2,`0`);case`W`:case`WW`:return i.s(t.isoWeek(),e===`W`?1:2,`0`);case`k`:case`kk`:return i.s(String(t.$H===0?24:t.$H),e===`k`?1:2,`0`);case`X`:return Math.floor(t.$d.getTime()/1e3);case`x`:return t.$d.getTime();case`z`:return`[`+t.offsetName()+`]`;case`zzz`:return`[`+t.offsetName(`long`)+`]`;default:return e}}));return r.bind(this)(a)}}}))})),Ge=l(((e,t)=>{(function(n,r){typeof e==`object`&&t!==void 0?t.exports=r():typeof define==`function`&&define.amd?define(r):(n=typeof globalThis<`u`?globalThis:n||self).dayjs_plugin_duration=r()})(e,(function(){var e,t,n=1e3,r=6e4,i=36e5,a=864e5,o=/\[([^\]]+)]|Y{1,4}|M{1,4}|D{1,2}|d{1,4}|H{1,2}|h{1,2}|a|A|m{1,2}|s{1,2}|Z{1,2}|SSS/g,s=31536e6,c=2628e6,l=/^(-|\+)?P(?:([-+]?[0-9,.]*)Y)?(?:([-+]?[0-9,.]*)M)?(?:([-+]?[0-9,.]*)W)?(?:([-+]?[0-9,.]*)D)?(?:T(?:([-+]?[0-9,.]*)H)?(?:([-+]?[0-9,.]*)M)?(?:([-+]?[0-9,.]*)S)?)?$/,u={years:s,months:c,days:a,hours:i,minutes:r,seconds:n,milliseconds:1,weeks:6048e5},d=function(e){return e instanceof v},f=function(e,t,n){return new v(e,n,t.$l)},p=function(e){return t.p(e)+`s`},m=function(e){return e<0},h=function(e){return m(e)?Math.ceil(e):Math.floor(e)},g=function(e){return Math.abs(e)},_=function(e,t){return e?m(e)?{negative:!0,format:``+g(e)+t}:{negative:!1,format:``+e+t}:{negative:!1,format:``}},v=function(){function m(e,t,n){var r=this;if(this.$d={},this.$l=n,e===void 0&&(this.$ms=0,this.parseFromMilliseconds()),t)return f(e*u[p(t)],this);if(typeof e==`number`)return this.$ms=e,this.parseFromMilliseconds(),this;if(typeof e==`object`)return Object.keys(e).forEach((function(t){r.$d[p(t)]=e[t]})),this.calMilliseconds(),this;if(typeof e==`string`){var i=e.match(l);if(i){var a=i.slice(2).map((function(e){return e==null?0:Number(e)}));return this.$d.years=a[0],this.$d.months=a[1],this.$d.weeks=a[2],this.$d.days=a[3],this.$d.hours=a[4],this.$d.minutes=a[5],this.$d.seconds=a[6],this.calMilliseconds(),this}}return this}var g=m.prototype;return g.calMilliseconds=function(){var e=this;this.$ms=Object.keys(this.$d).reduce((function(t,n){return t+(e.$d[n]||0)*u[n]}),0)},g.parseFromMilliseconds=function(){var e=this.$ms;this.$d.years=h(e/s),e%=s,this.$d.months=h(e/c),e%=c,this.$d.days=h(e/a),e%=a,this.$d.hours=h(e/i),e%=i,this.$d.minutes=h(e/r),e%=r,this.$d.seconds=h(e/n),e%=n,this.$d.milliseconds=e},g.toISOString=function(){var e=_(this.$d.years,`Y`),t=_(this.$d.months,`M`),n=+this.$d.days||0;this.$d.weeks&&(n+=7*this.$d.weeks);var r=_(n,`D`),i=_(this.$d.hours,`H`),a=_(this.$d.minutes,`M`),o=this.$d.seconds||0;this.$d.milliseconds&&(o+=this.$d.milliseconds/1e3,o=Math.round(1e3*o)/1e3);var s=_(o,`S`),c=e.negative||t.negative||r.negative||i.negative||a.negative||s.negative,l=i.format||a.format||s.format?`T`:``,u=(c?`-`:``)+`P`+e.format+t.format+r.format+l+i.format+a.format+s.format;return u===`P`||u===`-P`?`P0D`:u},g.toJSON=function(){return this.toISOString()},g.format=function(e){var n=e||`YYYY-MM-DDTHH:mm:ss`,r={Y:this.$d.years,YY:t.s(this.$d.years,2,`0`),YYYY:t.s(this.$d.years,4,`0`),M:this.$d.months,MM:t.s(this.$d.months,2,`0`),D:this.$d.days,DD:t.s(this.$d.days,2,`0`),H:this.$d.hours,HH:t.s(this.$d.hours,2,`0`),m:this.$d.minutes,mm:t.s(this.$d.minutes,2,`0`),s:this.$d.seconds,ss:t.s(this.$d.seconds,2,`0`),SSS:t.s(this.$d.milliseconds,3,`0`)};return n.replace(o,(function(e,t){return t||String(r[e])}))},g.as=function(e){return this.$ms/u[p(e)]},g.get=function(e){var t=this.$ms,n=p(e);return n===`milliseconds`?t%=1e3:t=n===`weeks`?h(t/u[n]):this.$d[n],t||0},g.add=function(e,t,n){var r;return r=t?e*u[p(t)]:d(e)?e.$ms:f(e,this).$ms,f(this.$ms+r*(n?-1:1),this)},g.subtract=function(e,t){return this.add(e,t,!0)},g.locale=function(e){var t=this.clone();return t.$l=e,t},g.clone=function(){return f(this.$ms,this)},g.humanize=function(t){return e().add(this.$ms,`ms`).locale(this.$l).fromNow(!t)},g.valueOf=function(){return this.asMilliseconds()},g.milliseconds=function(){return this.get(`milliseconds`)},g.asMilliseconds=function(){return this.as(`milliseconds`)},g.seconds=function(){return this.get(`seconds`)},g.asSeconds=function(){return this.as(`seconds`)},g.minutes=function(){return this.get(`minutes`)},g.asMinutes=function(){return this.as(`minutes`)},g.hours=function(){return this.get(`hours`)},g.asHours=function(){return this.as(`hours`)},g.days=function(){return this.get(`days`)},g.asDays=function(){return this.as(`days`)},g.weeks=function(){return this.get(`weeks`)},g.asWeeks=function(){return this.as(`weeks`)},g.months=function(){return this.get(`months`)},g.asMonths=function(){return this.as(`months`)},g.years=function(){return this.get(`years`)},g.asYears=function(){return this.as(`years`)},m}(),y=function(e,t,n){return e.add(t.years()*n,`y`).add(t.months()*n,`M`).add(t.days()*n,`d`).add(t.hours()*n,`h`).add(t.minutes()*n,`m`).add(t.seconds()*n,`s`).add(t.milliseconds()*n,`ms`)};return function(n,r,i){e=i,t=i().$utils(),i.duration=function(e,t){return f(e,{$l:i.locale()},t)},i.isDuration=d;var a=r.prototype.add,o=r.prototype.subtract;r.prototype.add=function(e,t){return d(e)?y(this,e,1):a.bind(this)(e,t)},r.prototype.subtract=function(e,t){return d(e)?y(this,e,-1):o.bind(this)(e,t)}}}))})),Ke=C(),R=n(e(),1),qe=n(He(),1),Je=n(Ue(),1),Ye=n(We(),1),Xe=n(Ge(),1),Ze=(function(){var e=t(function(e,t,n,r){for(n||={},r=e.length;r--;n[e[r]]=t);return n},`o`),n=[6,8,10,12,13,14,15,16,17,18,20,21,22,23,24,25,26,27,28,29,30,31,33,35,36,38,40],r=[1,26],i=[1,27],a=[1,28],o=[1,29],s=[1,30],c=[1,31],l=[1,32],u=[1,33],d=[1,34],f=[1,9],p=[1,10],m=[1,11],h=[1,12],g=[1,13],_=[1,14],v=[1,15],y=[1,16],b=[1,19],x=[1,20],S=[1,21],C=[1,22],w=[1,23],T=[1,25],E=[1,35],D={trace:t(function(){},`trace`),yy:{},symbols_:{error:2,start:3,gantt:4,document:5,EOF:6,line:7,SPACE:8,statement:9,NL:10,weekday:11,weekday_monday:12,weekday_tuesday:13,weekday_wednesday:14,weekday_thursday:15,weekday_friday:16,weekday_saturday:17,weekday_sunday:18,weekend:19,weekend_friday:20,weekend_saturday:21,dateFormat:22,inclusiveEndDates:23,topAxis:24,axisFormat:25,tickInterval:26,excludes:27,includes:28,todayMarker:29,title:30,acc_title:31,acc_title_value:32,acc_descr:33,acc_descr_value:34,acc_descr_multiline_value:35,section:36,clickStatement:37,taskTxt:38,taskData:39,click:40,callbackname:41,callbackargs:42,href:43,clickStatementDebug:44,$accept:0,$end:1},terminals_:{2:`error`,4:`gantt`,6:`EOF`,8:`SPACE`,10:`NL`,12:`weekday_monday`,13:`weekday_tuesday`,14:`weekday_wednesday`,15:`weekday_thursday`,16:`weekday_friday`,17:`weekday_saturday`,18:`weekday_sunday`,20:`weekend_friday`,21:`weekend_saturday`,22:`dateFormat`,23:`inclusiveEndDates`,24:`topAxis`,25:`axisFormat`,26:`tickInterval`,27:`excludes`,28:`includes`,29:`todayMarker`,30:`title`,31:`acc_title`,32:`acc_title_value`,33:`acc_descr`,34:`acc_descr_value`,35:`acc_descr_multiline_value`,36:`section`,38:`taskTxt`,39:`taskData`,40:`click`,41:`callbackname`,42:`callbackargs`,43:`href`},productions_:[0,[3,3],[5,0],[5,2],[7,2],[7,1],[7,1],[7,1],[11,1],[11,1],[11,1],[11,1],[11,1],[11,1],[11,1],[19,1],[19,1],[9,1],[9,1],[9,1],[9,1],[9,1],[9,1],[9,1],[9,1],[9,1],[9,1],[9,1],[9,2],[9,2],[9,1],[9,1],[9,1],[9,2],[37,2],[37,3],[37,3],[37,4],[37,3],[37,4],[37,2],[44,2],[44,3],[44,3],[44,4],[44,3],[44,4],[44,2]],performAction:t(function(e,t,n,r,i,a,o){var s=a.length-1;switch(i){case 1:return a[s-1];case 2:this.$=[];break;case 3:a[s-1].push(a[s]),this.$=a[s-1];break;case 4:case 5:this.$=a[s];break;case 6:case 7:this.$=[];break;case 8:r.setWeekday(`monday`);break;case 9:r.setWeekday(`tuesday`);break;case 10:r.setWeekday(`wednesday`);break;case 11:r.setWeekday(`thursday`);break;case 12:r.setWeekday(`friday`);break;case 13:r.setWeekday(`saturday`);break;case 14:r.setWeekday(`sunday`);break;case 15:r.setWeekend(`friday`);break;case 16:r.setWeekend(`saturday`);break;case 17:r.setDateFormat(a[s].substr(11)),this.$=a[s].substr(11);break;case 18:r.enableInclusiveEndDates(),this.$=a[s].substr(18);break;case 19:r.TopAxis(),this.$=a[s].substr(8);break;case 20:r.setAxisFormat(a[s].substr(11)),this.$=a[s].substr(11);break;case 21:r.setTickInterval(a[s].substr(13)),this.$=a[s].substr(13);break;case 22:r.setExcludes(a[s].substr(9)),this.$=a[s].substr(9);break;case 23:r.setIncludes(a[s].substr(9)),this.$=a[s].substr(9);break;case 24:r.setTodayMarker(a[s].substr(12)),this.$=a[s].substr(12);break;case 27:r.setDiagramTitle(a[s].substr(6)),this.$=a[s].substr(6);break;case 28:this.$=a[s].trim(),r.setAccTitle(this.$);break;case 29:case 30:this.$=a[s].trim(),r.setAccDescription(this.$);break;case 31:r.addSection(a[s].substr(8)),this.$=a[s].substr(8);break;case 33:r.addTask(a[s-1],a[s]),this.$=`task`;break;case 34:this.$=a[s-1],r.setClickEvent(a[s-1],a[s],null);break;case 35:this.$=a[s-2],r.setClickEvent(a[s-2],a[s-1],a[s]);break;case 36:this.$=a[s-2],r.setClickEvent(a[s-2],a[s-1],null),r.setLink(a[s-2],a[s]);break;case 37:this.$=a[s-3],r.setClickEvent(a[s-3],a[s-2],a[s-1]),r.setLink(a[s-3],a[s]);break;case 38:this.$=a[s-2],r.setClickEvent(a[s-2],a[s],null),r.setLink(a[s-2],a[s-1]);break;case 39:this.$=a[s-3],r.setClickEvent(a[s-3],a[s-1],a[s]),r.setLink(a[s-3],a[s-2]);break;case 40:this.$=a[s-1],r.setLink(a[s-1],a[s]);break;case 41:case 47:this.$=a[s-1]+` `+a[s];break;case 42:case 43:case 45:this.$=a[s-2]+` `+a[s-1]+` `+a[s];break;case 44:case 46:this.$=a[s-3]+` `+a[s-2]+` `+a[s-1]+` `+a[s];break}},`anonymous`),table:[{3:1,4:[1,2]},{1:[3]},e(n,[2,2],{5:3}),{6:[1,4],7:5,8:[1,6],9:7,10:[1,8],11:17,12:r,13:i,14:a,15:o,16:s,17:c,18:l,19:18,20:u,21:d,22:f,23:p,24:m,25:h,26:g,27:_,28:v,29:y,30:b,31:x,33:S,35:C,36:w,37:24,38:T,40:E},e(n,[2,7],{1:[2,1]}),e(n,[2,3]),{9:36,11:17,12:r,13:i,14:a,15:o,16:s,17:c,18:l,19:18,20:u,21:d,22:f,23:p,24:m,25:h,26:g,27:_,28:v,29:y,30:b,31:x,33:S,35:C,36:w,37:24,38:T,40:E},e(n,[2,5]),e(n,[2,6]),e(n,[2,17]),e(n,[2,18]),e(n,[2,19]),e(n,[2,20]),e(n,[2,21]),e(n,[2,22]),e(n,[2,23]),e(n,[2,24]),e(n,[2,25]),e(n,[2,26]),e(n,[2,27]),{32:[1,37]},{34:[1,38]},e(n,[2,30]),e(n,[2,31]),e(n,[2,32]),{39:[1,39]},e(n,[2,8]),e(n,[2,9]),e(n,[2,10]),e(n,[2,11]),e(n,[2,12]),e(n,[2,13]),e(n,[2,14]),e(n,[2,15]),e(n,[2,16]),{41:[1,40],43:[1,41]},e(n,[2,4]),e(n,[2,28]),e(n,[2,29]),e(n,[2,33]),e(n,[2,34],{42:[1,42],43:[1,43]}),e(n,[2,40],{41:[1,44]}),e(n,[2,35],{43:[1,45]}),e(n,[2,36]),e(n,[2,38],{42:[1,46]}),e(n,[2,37]),e(n,[2,39])],defaultActions:{},parseError:t(function(e,t){if(t.recoverable)this.trace(e);else{var n=Error(e);throw n.hash=t,n}},`parseError`),parse:t(function(e){var n=this,r=[0],i=[],a=[null],o=[],s=this.table,c=``,l=0,u=0,d=0,f=2,p=1,m=o.slice.call(arguments,1),h=Object.create(this.lexer),g={yy:{}};for(var _ in this.yy)Object.prototype.hasOwnProperty.call(this.yy,_)&&(g.yy[_]=this.yy[_]);h.setInput(e,g.yy),g.yy.lexer=h,g.yy.parser=this,h.yylloc===void 0&&(h.yylloc={});var v=h.yylloc;o.push(v);var y=h.options&&h.options.ranges;typeof g.yy.parseError==`function`?this.parseError=g.yy.parseError:this.parseError=Object.getPrototypeOf(this).parseError;function b(e){r.length-=2*e,a.length-=e,o.length-=e}t(b,`popStack`);function x(){var e=i.pop()||h.lex()||p;return typeof e!=`number`&&(e instanceof Array&&(i=e,e=i.pop()),e=n.symbols_[e]||e),e}t(x,`lex`);for(var S,C,w,T,E,D={},O,k,A,j;;){if(w=r[r.length-1],this.defaultActions[w]?T=this.defaultActions[w]:(S??=x(),T=s[w]&&s[w][S]),T===void 0||!T.length||!T[0]){var ee=``;for(O in j=[],s[w])this.terminals_[O]&&O>f&&j.push(`'`+this.terminals_[O]+`'`);ee=h.showPosition?`Parse error on line `+(l+1)+`:
`+h.showPosition()+`
Expecting `+j.join(`, `)+`, got '`+(this.terminals_[S]||S)+`'`:`Parse error on line `+(l+1)+`: Unexpected `+(S==p?`end of input`:`'`+(this.terminals_[S]||S)+`'`),this.parseError(ee,{text:h.match,token:this.terminals_[S]||S,line:h.yylineno,loc:v,expected:j})}if(T[0]instanceof Array&&T.length>1)throw Error(`Parse Error: multiple actions possible at state: `+w+`, token: `+S);switch(T[0]){case 1:r.push(S),a.push(h.yytext),o.push(h.yylloc),r.push(T[1]),S=null,C?(S=C,C=null):(u=h.yyleng,c=h.yytext,l=h.yylineno,v=h.yylloc,d>0&&d--);break;case 2:if(k=this.productions_[T[1]][1],D.$=a[a.length-k],D._$={first_line:o[o.length-(k||1)].first_line,last_line:o[o.length-1].last_line,first_column:o[o.length-(k||1)].first_column,last_column:o[o.length-1].last_column},y&&(D._$.range=[o[o.length-(k||1)].range[0],o[o.length-1].range[1]]),E=this.performAction.apply(D,[c,u,l,g.yy,T[1],a,o].concat(m)),E!==void 0)return E;k&&(r=r.slice(0,-1*k*2),a=a.slice(0,-1*k),o=o.slice(0,-1*k)),r.push(this.productions_[T[1]][0]),a.push(D.$),o.push(D._$),A=s[r[r.length-2]][r[r.length-1]],r.push(A);break;case 3:return!0}}return!0},`parse`)};D.lexer=(function(){return{EOF:1,parseError:t(function(e,t){if(this.yy.parser)this.yy.parser.parseError(e,t);else throw Error(e)},`parseError`),setInput:t(function(e,t){return this.yy=t||this.yy||{},this._input=e,this._more=this._backtrack=this.done=!1,this.yylineno=this.yyleng=0,this.yytext=this.matched=this.match=``,this.conditionStack=[`INITIAL`],this.yylloc={first_line:1,first_column:0,last_line:1,last_column:0},this.options.ranges&&(this.yylloc.range=[0,0]),this.offset=0,this},`setInput`),input:t(function(){var e=this._input[0];return this.yytext+=e,this.yyleng++,this.offset++,this.match+=e,this.matched+=e,e.match(/(?:\r\n?|\n).*/g)?(this.yylineno++,this.yylloc.last_line++):this.yylloc.last_column++,this.options.ranges&&this.yylloc.range[1]++,this._input=this._input.slice(1),e},`input`),unput:t(function(e){var t=e.length,n=e.split(/(?:\r\n?|\n)/g);this._input=e+this._input,this.yytext=this.yytext.substr(0,this.yytext.length-t),this.offset-=t;var r=this.match.split(/(?:\r\n?|\n)/g);this.match=this.match.substr(0,this.match.length-1),this.matched=this.matched.substr(0,this.matched.length-1),n.length-1&&(this.yylineno-=n.length-1);var i=this.yylloc.range;return this.yylloc={first_line:this.yylloc.first_line,last_line:this.yylineno+1,first_column:this.yylloc.first_column,last_column:n?(n.length===r.length?this.yylloc.first_column:0)+r[r.length-n.length].length-n[0].length:this.yylloc.first_column-t},this.options.ranges&&(this.yylloc.range=[i[0],i[0]+this.yyleng-t]),this.yyleng=this.yytext.length,this},`unput`),more:t(function(){return this._more=!0,this},`more`),reject:t(function(){if(this.options.backtrack_lexer)this._backtrack=!0;else return this.parseError(`Lexical error on line `+(this.yylineno+1)+`. You can only invoke reject() in the lexer when the lexer is of the backtracking persuasion (options.backtrack_lexer = true).
`+this.showPosition(),{text:``,token:null,line:this.yylineno});return this},`reject`),less:t(function(e){this.unput(this.match.slice(e))},`less`),pastInput:t(function(){var e=this.matched.substr(0,this.matched.length-this.match.length);return(e.length>20?`...`:``)+e.substr(-20).replace(/\n/g,``)},`pastInput`),upcomingInput:t(function(){var e=this.match;return e.length<20&&(e+=this._input.substr(0,20-e.length)),(e.substr(0,20)+(e.length>20?`...`:``)).replace(/\n/g,``)},`upcomingInput`),showPosition:t(function(){var e=this.pastInput(),t=Array(e.length+1).join(`-`);return e+this.upcomingInput()+`
`+t+`^`},`showPosition`),test_match:t(function(e,t){var n,r,i;if(this.options.backtrack_lexer&&(i={yylineno:this.yylineno,yylloc:{first_line:this.yylloc.first_line,last_line:this.last_line,first_column:this.yylloc.first_column,last_column:this.yylloc.last_column},yytext:this.yytext,match:this.match,matches:this.matches,matched:this.matched,yyleng:this.yyleng,offset:this.offset,_more:this._more,_input:this._input,yy:this.yy,conditionStack:this.conditionStack.slice(0),done:this.done},this.options.ranges&&(i.yylloc.range=this.yylloc.range.slice(0))),r=e[0].match(/(?:\r\n?|\n).*/g),r&&(this.yylineno+=r.length),this.yylloc={first_line:this.yylloc.last_line,last_line:this.yylineno+1,first_column:this.yylloc.last_column,last_column:r?r[r.length-1].length-r[r.length-1].match(/\r?\n?/)[0].length:this.yylloc.last_column+e[0].length},this.yytext+=e[0],this.match+=e[0],this.matches=e,this.yyleng=this.yytext.length,this.options.ranges&&(this.yylloc.range=[this.offset,this.offset+=this.yyleng]),this._more=!1,this._backtrack=!1,this._input=this._input.slice(e[0].length),this.matched+=e[0],n=this.performAction.call(this,this.yy,this,t,this.conditionStack[this.conditionStack.length-1]),this.done&&this._input&&(this.done=!1),n)return n;if(this._backtrack){for(var a in i)this[a]=i[a];return!1}return!1},`test_match`),next:t(function(){if(this.done)return this.EOF;this._input||(this.done=!0);var e,t,n,r;this._more||(this.yytext=``,this.match=``);for(var i=this._currentRules(),a=0;a<i.length;a++)if(n=this._input.match(this.rules[i[a]]),n&&(!t||n[0].length>t[0].length)){if(t=n,r=a,this.options.backtrack_lexer){if(e=this.test_match(n,i[a]),e!==!1)return e;if(this._backtrack){t=!1;continue}else return!1}else if(!this.options.flex)break}return t?(e=this.test_match(t,i[r]),e===!1?!1:e):this._input===``?this.EOF:this.parseError(`Lexical error on line `+(this.yylineno+1)+`. Unrecognized text.
`+this.showPosition(),{text:``,token:null,line:this.yylineno})},`next`),lex:t(function(){return this.next()||this.lex()},`lex`),begin:t(function(e){this.conditionStack.push(e)},`begin`),popState:t(function(){return this.conditionStack.length-1>0?this.conditionStack.pop():this.conditionStack[0]},`popState`),_currentRules:t(function(){return this.conditionStack.length&&this.conditionStack[this.conditionStack.length-1]?this.conditions[this.conditionStack[this.conditionStack.length-1]].rules:this.conditions.INITIAL.rules},`_currentRules`),topState:t(function(e){return e=this.conditionStack.length-1-Math.abs(e||0),e>=0?this.conditionStack[e]:`INITIAL`},`topState`),pushState:t(function(e){this.begin(e)},`pushState`),stateStackSize:t(function(){return this.conditionStack.length},`stateStackSize`),options:{"case-insensitive":!0},performAction:t(function(e,t,n,r){switch(n){case 0:return this.begin(`open_directive`),`open_directive`;case 1:return this.begin(`acc_title`),31;case 2:return this.popState(),`acc_title_value`;case 3:return this.begin(`acc_descr`),33;case 4:return this.popState(),`acc_descr_value`;case 5:this.begin(`acc_descr_multiline`);break;case 6:this.popState();break;case 7:return`acc_descr_multiline_value`;case 8:break;case 9:break;case 10:break;case 11:return 10;case 12:break;case 13:break;case 14:this.begin(`href`);break;case 15:this.popState();break;case 16:return 43;case 17:this.begin(`callbackname`);break;case 18:this.popState();break;case 19:this.popState(),this.begin(`callbackargs`);break;case 20:return 41;case 21:this.popState();break;case 22:return 42;case 23:this.begin(`click`);break;case 24:this.popState();break;case 25:return 40;case 26:return 4;case 27:return 22;case 28:return 23;case 29:return 24;case 30:return 25;case 31:return 26;case 32:return 28;case 33:return 27;case 34:return 29;case 35:return 12;case 36:return 13;case 37:return 14;case 38:return 15;case 39:return 16;case 40:return 17;case 41:return 18;case 42:return 20;case 43:return 21;case 44:return`date`;case 45:return 30;case 46:return`accDescription`;case 47:return 36;case 48:return 38;case 49:return 39;case 50:return`:`;case 51:return 6;case 52:return`INVALID`}},`anonymous`),rules:[/^(?:%%\{)/i,/^(?:accTitle\s*:\s*)/i,/^(?:(?!\n||)*[^\n]*)/i,/^(?:accDescr\s*:\s*)/i,/^(?:(?!\n||)*[^\n]*)/i,/^(?:accDescr\s*\{\s*)/i,/^(?:[\}])/i,/^(?:[^\}]*)/i,/^(?:%%(?!\{)*[^\n]*)/i,/^(?:[^\}]%%*[^\n]*)/i,/^(?:%%*[^\n]*[\n]*)/i,/^(?:[\n]+)/i,/^(?:\s+)/i,/^(?:%[^\n]*)/i,/^(?:href[\s]+["])/i,/^(?:["])/i,/^(?:[^"]*)/i,/^(?:call[\s]+)/i,/^(?:\([\s]*\))/i,/^(?:\()/i,/^(?:[^(]*)/i,/^(?:\))/i,/^(?:[^)]*)/i,/^(?:click[\s]+)/i,/^(?:[\s\n])/i,/^(?:[^\s\n]*)/i,/^(?:gantt\b)/i,/^(?:dateFormat\s[^#\n;]+)/i,/^(?:inclusiveEndDates\b)/i,/^(?:topAxis\b)/i,/^(?:axisFormat\s[^#\n;]+)/i,/^(?:tickInterval\s[^#\n;]+)/i,/^(?:includes\s[^#\n;]+)/i,/^(?:excludes\s[^#\n;]+)/i,/^(?:todayMarker\s[^\n;]+)/i,/^(?:weekday\s+monday\b)/i,/^(?:weekday\s+tuesday\b)/i,/^(?:weekday\s+wednesday\b)/i,/^(?:weekday\s+thursday\b)/i,/^(?:weekday\s+friday\b)/i,/^(?:weekday\s+saturday\b)/i,/^(?:weekday\s+sunday\b)/i,/^(?:weekend\s+friday\b)/i,/^(?:weekend\s+saturday\b)/i,/^(?:\d\d\d\d-\d\d-\d\d\b)/i,/^(?:title\s[^\n]+)/i,/^(?:accDescription\s[^#\n;]+)/i,/^(?:section\s[^\n]+)/i,/^(?:[^:\n]+)/i,/^(?::[^#\n;]+)/i,/^(?::)/i,/^(?:$)/i,/^(?:.)/i],conditions:{acc_descr_multiline:{rules:[6,7],inclusive:!1},acc_descr:{rules:[4],inclusive:!1},acc_title:{rules:[2],inclusive:!1},callbackargs:{rules:[21,22],inclusive:!1},callbackname:{rules:[18,19,20],inclusive:!1},href:{rules:[15,16],inclusive:!1},click:{rules:[24,25],inclusive:!1},INITIAL:{rules:[0,1,3,5,8,9,10,11,12,13,14,17,23,26,27,28,29,30,31,32,33,34,35,36,37,38,39,40,41,42,43,44,45,46,47,48,49,50,51,52],inclusive:!0}}}})();function O(){this.yy={}}return t(O,`Parser`),O.prototype=D,D.Parser=O,new O})();Ze.parser=Ze;var Qe=Ze;R.default.extend(qe.default),R.default.extend(Je.default),R.default.extend(Ye.default);var $e={friday:5,saturday:6},z=``,B=``,et=void 0,tt=``,V=[],H=[],nt=new Map,rt=[],U=[],W=``,it=``,at=[`active`,`done`,`crit`,`milestone`,`vert`],ot=[],G=``,K=!1,st=!1,ct=`sunday`,q=`saturday`,lt=0,ut=t(function(){rt=[],U=[],W=``,ot=[],Ut=0,Kt=void 0,Y=void 0,X=[],z=``,B=``,it=``,et=void 0,tt=``,V=[],H=[],K=!1,st=!1,lt=0,nt=new Map,G=``,g(),ct=`sunday`,q=`saturday`},`clear`),dt=t(function(e){G=e},`setDiagramId`),ft=t(function(e){B=e},`setAxisFormat`),pt=t(function(){return B},`getAxisFormat`),mt=t(function(e){et=e},`setTickInterval`),ht=t(function(){return et},`getTickInterval`),gt=t(function(e){tt=e},`setTodayMarker`),_t=t(function(){return tt},`getTodayMarker`),vt=t(function(e){z=e},`setDateFormat`),yt=t(function(){K=!0},`enableInclusiveEndDates`),bt=t(function(){return K},`endDatesAreInclusive`),xt=t(function(){st=!0},`enableTopAxis`),St=t(function(){return st},`topAxisEnabled`),Ct=t(function(e){it=e},`setDisplayMode`),wt=t(function(){return it},`getDisplayMode`),Tt=t(function(){return z},`getDateFormat`),Et=t(function(e){V=e.toLowerCase().split(/[\s,]+/)},`setIncludes`),Dt=t(function(){return V},`getIncludes`),Ot=t(function(e){H=e.toLowerCase().split(/[\s,]+/)},`setExcludes`),kt=t(function(){return H},`getExcludes`),At=t(function(){return nt},`getLinks`),jt=t(function(e){W=e,rt.push(e)},`addSection`),Mt=t(function(){return rt},`getSections`),Nt=t(function(){let e=Xt(),t=0;for(;!e&&t<10;)e=Xt(),t++;return U=X,U},`getTasks`),Pt=t(function(e,t,n,r){let i=e.format(t.trim()),a=e.format(`YYYY-MM-DD`);return r.includes(i)||r.includes(a)?!1:n.includes(`weekends`)&&(e.isoWeekday()===$e[q]||e.isoWeekday()===$e[q]+1)||n.includes(e.format(`dddd`).toLowerCase())?!0:n.includes(i)||n.includes(a)},`isInvalidDate`),Ft=t(function(e){ct=e},`setWeekday`),It=t(function(){return ct},`getWeekday`),Lt=t(function(e){q=e},`setWeekend`),Rt=t(function(e,t,n,r){if(!n.length||e.manualEndTime)return;let i;i=e.startTime instanceof Date?(0,R.default)(e.startTime):(0,R.default)(e.startTime,t,!0),i=i.add(1,`d`);let a;a=e.endTime instanceof Date?(0,R.default)(e.endTime):(0,R.default)(e.endTime,t,!0);let[o,s]=zt(i,a,t,n,r);e.endTime=o.toDate(),e.renderEndTime=s},`checkTaskDates`),zt=t(function(e,t,n,r,i){let a=!1,o=null,s=t.add(1e4,`d`);for(;e<=t;){if(a||(o=t.toDate()),a=Pt(e,n,r,i),a&&(t=t.add(1,`d`),t>s))throw Error("Failed to find a valid date that was not excluded by `excludes` after 10,000 iterations.");e=e.add(1,`d`)}return[t,o]},`fixTaskDates`),Bt=t(function(e,n,i){if(i=i.trim(),t(e=>{let t=e.trim();return t===`x`||t===`X`},`isTimestampFormat`)(n)&&/^\d+$/.test(i))return new Date(Number(i));let a=/^after\s+(?<ids>[\d\w- ]+)/.exec(i);if(a!==null){let e=null;for(let t of a.groups.ids.split(` `)){let n=Z(t);n!==void 0&&(!e||n.endTime>e.endTime)&&(e=n)}if(e)return e.endTime;let t=new Date;return t.setHours(0,0,0,0),t}let o=(0,R.default)(i,n.trim(),!0);if(o.isValid())return o.toDate();{r.debug(`Invalid date:`+i),r.debug(`With date format:`+n.trim());let e=new Date(i);if(e===void 0||isNaN(e.getTime())||e.getFullYear()<-1e4||e.getFullYear()>1e4)throw Error(`Invalid date:`+i);return e}},`getStartDate`),Vt=t(function(e){let t=/^(\d+(?:\.\d+)?)([Mdhmswy]|ms)$/.exec(e.trim());return t===null?[NaN,`ms`]:[Number.parseFloat(t[1]),t[2]]},`parseDuration`),Ht=t(function(e,t,n,r=!1){n=n.trim();let i=/^until\s+(?<ids>[\d\w- ]+)/.exec(n);if(i!==null){let e=null;for(let t of i.groups.ids.split(` `)){let n=Z(t);n!==void 0&&(!e||n.startTime<e.startTime)&&(e=n)}if(e)return e.startTime;let t=new Date;return t.setHours(0,0,0,0),t}let a=(0,R.default)(n,t.trim(),!0);if(a.isValid())return r&&(a=a.add(1,`d`)),a.toDate();let o=(0,R.default)(e),[s,c]=Vt(n);if(!Number.isNaN(s)){let e=o.add(s,c);e.isValid()&&(o=e)}return o.toDate()},`getEndDate`),Ut=0,J=t(function(e){return e===void 0?(Ut+=1,`task`+Ut):e},`parseId`),Wt=t(function(e,t){let n;n=t.substr(0,1)===`:`?t.substr(1,t.length):t;let r=n.split(`,`),i={};Q(r,i,at);for(let e=0;e<r.length;e++)r[e]=r[e].trim();let a=``;switch(r.length){case 1:i.id=J(),i.startTime=e.endTime,a=r[0];break;case 2:i.id=J(),i.startTime=Bt(void 0,z,r[0]),a=r[1];break;case 3:i.id=J(r[0]),i.startTime=Bt(void 0,z,r[1]),a=r[2];break;default:}return a&&(i.endTime=Ht(i.startTime,z,a,K),i.manualEndTime=(0,R.default)(a,`YYYY-MM-DD`,!0).isValid(),Rt(i,z,H,V)),i},`compileData`),Gt=t(function(e,t){let n;n=t.substr(0,1)===`:`?t.substr(1,t.length):t;let r=n.split(`,`),i={};Q(r,i,at);for(let e=0;e<r.length;e++)r[e]=r[e].trim();switch(r.length){case 1:i.id=J(),i.startTime={type:`prevTaskEnd`,id:e},i.endTime={data:r[0]};break;case 2:i.id=J(),i.startTime={type:`getStartDate`,startData:r[0]},i.endTime={data:r[1]};break;case 3:i.id=J(r[0]),i.startTime={type:`getStartDate`,startData:r[1]},i.endTime={data:r[2]};break;default:}return i},`parseData`),Kt,Y,X=[],qt={},Jt=t(function(e,t){let n={section:W,type:W,processed:!1,manualEndTime:!1,renderEndTime:null,raw:{data:t},task:e,classes:[]},r=Gt(Y,t);n.raw.startTime=r.startTime,n.raw.endTime=r.endTime,n.id=r.id,n.prevTaskId=Y,n.active=r.active,n.done=r.done,n.crit=r.crit,n.milestone=r.milestone,n.vert=r.vert,n.order=lt,lt++;let i=X.push(n);Y=n.id,qt[n.id]=i-1},`addTask`),Z=t(function(e){let t=qt[e];return X[t]},`findTaskById`),Yt=t(function(e,t){let n={section:W,type:W,description:e,task:e,classes:[]},r=Wt(Kt,t);n.startTime=r.startTime,n.endTime=r.endTime,n.id=r.id,n.active=r.active,n.done=r.done,n.crit=r.crit,n.milestone=r.milestone,n.vert=r.vert,Kt=n,U.push(n)},`addTaskOrg`),Xt=t(function(){let e=t(function(e){let t=X[e],n=``;switch(X[e].raw.startTime.type){case`prevTaskEnd`:t.startTime=Z(t.prevTaskId).endTime;break;case`getStartDate`:n=Bt(void 0,z,X[e].raw.startTime.startData),n&&(X[e].startTime=n);break}return X[e].startTime&&(X[e].endTime=Ht(X[e].startTime,z,X[e].raw.endTime.data,K),X[e].endTime&&(X[e].processed=!0,X[e].manualEndTime=(0,R.default)(X[e].raw.endTime.data,`YYYY-MM-DD`,!0).isValid(),Rt(X[e],z,H,V))),X[e].processed},`compileTask`),n=!0;for(let[t,r]of X.entries())e(t),n&&=r.processed;return n},`compileTasks`),Zt=t(function(e,t){let n=t;x().securityLevel!==`loose`&&(n=(0,Ke.sanitizeUrl)(t)),e.split(`,`).forEach(function(e){Z(e)!==void 0&&(en(e,()=>{window.open(n,`_self`)}),nt.set(e,n))}),Qt(e,`clickable`)},`setLink`),Qt=t(function(e,t){e.split(`,`).forEach(function(e){let n=Z(e);n!==void 0&&n.classes.push(t)})},`setClass`),$t=t(function(e,t,n){if(x().securityLevel!==`loose`||t===void 0)return;let r=[];if(typeof n==`string`){r=n.split(/,(?=(?:(?:[^"]*"){2})*[^"]*$)/);for(let e=0;e<r.length;e++){let t=r[e].trim();t.startsWith(`"`)&&t.endsWith(`"`)&&(t=t.substr(1,t.length-2)),r[e]=t}}r.length===0&&r.push(e),Z(e)!==void 0&&en(e,()=>{w.runFunc(t,...r)})},`setClickFun`),en=t(function(e,t){ot.push(function(){let n=G?`${G}-${e}`:e,r=document.querySelector(`[id="${n}"]`);r!==null&&r.addEventListener(`click`,function(){t()})},function(){let n=G?`${G}-${e}`:e,r=document.querySelector(`[id="${n}-text"]`);r!==null&&r.addEventListener(`click`,function(){t()})})},`pushFun`),tn={getConfig:t(()=>x().gantt,`getConfig`),clear:ut,setDateFormat:vt,getDateFormat:Tt,enableInclusiveEndDates:yt,endDatesAreInclusive:bt,enableTopAxis:xt,topAxisEnabled:St,setAxisFormat:ft,getAxisFormat:pt,setTickInterval:mt,getTickInterval:ht,setTodayMarker:gt,getTodayMarker:_t,setAccTitle:h,getAccTitle:S,setDiagramTitle:m,getDiagramTitle:b,setDiagramId:dt,setDisplayMode:Ct,getDisplayMode:wt,setAccDescription:p,getAccDescription:y,addSection:jt,getSections:Mt,getTasks:Nt,addTask:Jt,findTaskById:Z,addTaskOrg:Yt,setIncludes:Et,getIncludes:Dt,setExcludes:Ot,getExcludes:kt,setClickEvent:t(function(e,t,n){e.split(`,`).forEach(function(e){$t(e,t,n)}),Qt(e,`clickable`)},`setClickEvent`),setLink:Zt,getLinks:At,bindFunctions:t(function(e){ot.forEach(function(t){t(e)})},`bindFunctions`),parseDuration:Vt,isInvalidDate:Pt,setWeekday:Ft,getWeekday:It,setWeekend:Lt};function Q(e,t,n){let r=!0;for(;r;)r=!1,n.forEach(function(n){let i=`^\\s*`+n+`\\s*$`,a=new RegExp(i);e[0].match(a)&&(t[n]=!0,e.shift(1),r=!0)})}t(Q,`getTaskTags`),R.default.extend(Xe.default);var nn=t(function(){r.debug(`Something is calling, setConf, remove the call`)},`setConf`),rn={monday:re,tuesday:ue,wednesday:j,thursday:se,friday:ae,saturday:ne,sunday:A},an=t((e,t)=>{let n=[...e].map(()=>-1/0),r=[...e].sort((e,t)=>e.startTime-t.startTime||e.order-t.order),i=0;for(let e of r)for(let r=0;r<n.length;r++)if(e.startTime>=n[r]){n[r]=e.endTime,e.order=r+t,r>i&&(i=r);break}return i},`getMaxIntersections`),$,on=1e4,sn={parser:Qe,db:tn,renderer:{setConf:nn,draw:t(function(e,n,i,a){let o=x().gantt;a.db.setDiagramId(n);let s=x().securityLevel,c;s===`sandbox`&&(c=f(`#i`+n));let l=f(s===`sandbox`?c.nodes()[0].contentDocument.body:`body`),u=s===`sandbox`?c.nodes()[0].contentDocument:document,d=u.getElementById(n);$=d.parentElement.offsetWidth,$===void 0&&($=1200),o.useWidth!==void 0&&($=o.useWidth);let p=a.db.getTasks(),m=[];for(let e of p)m.push(e.type);m=se(m);let h={},g=2*o.topPadding;if(a.db.getDisplayMode()===`compact`||o.displayMode===`compact`){let e={};for(let t of p)e[t.section]===void 0?e[t.section]=[t]:e[t.section].push(t);let t=0;for(let n of Object.keys(e)){let r=an(e[n],t)+1;t+=r,g+=r*(o.barHeight+o.barGap),h[n]=r}}else{g+=p.length*(o.barHeight+o.barGap);for(let e of m)h[e]=p.filter(t=>t.type===e).length}d.setAttribute(`viewBox`,`0 0 `+$+` `+g);let y=l.select(`[id="${n}"]`),b=ee().domain([T(p,function(e){return e.startTime}),ie(p,function(e){return e.endTime})]).rangeRound([0,$-o.leftPadding-o.rightPadding]);function S(e,t){let n=e.startTime,r=t.startTime,i=0;return n>r?i=1:n<r&&(i=-1),i}t(S,`taskCompare`),p.sort(S),C(p,$,g),_(y,g,$,o.useMaxWidth),y.append(`text`).text(a.db.getDiagramTitle()).attr(`x`,$/2).attr(`y`,o.titleTopMargin).attr(`class`,`titleText`);function C(e,t,n){let r=o.barHeight,i=r+o.barGap,s=o.topPadding,c=o.leftPadding,l=D().domain([0,m.length]).range([`#00B9FA`,`#F95002`]).interpolate(Ve);A(i,s,c,t,n,e,a.db.getExcludes(),a.db.getIncludes()),ne(c,s,t,n),w(e,i,s,c,r,l,t,n),re(i,s,c,r,l),ae(c,s,t,n)}t(C,`makeGantt`);function w(e,t,r,i,s,c,l){e.sort((e,t)=>e.vert===t.vert?0:e.vert?1:-1);let u=[...new Set(e.map(e=>e.order))].map(t=>e.find(e=>e.order===t));y.append(`g`).selectAll(`rect`).data(u).enter().append(`rect`).attr(`x`,0).attr(`y`,function(e,n){return n=e.order,n*t+r-2}).attr(`width`,function(){return l-o.rightPadding/2}).attr(`height`,t).attr(`class`,function(e){for(let[t,n]of m.entries())if(e.type===n)return`section section`+t%o.numberSectionStyles;return`section section0`}).enter();let d=y.append(`g`).selectAll(`rect`).data(e).enter(),h=a.db.getLinks();if(d.append(`rect`).attr(`id`,function(e){return n+`-`+e.id}).attr(`rx`,3).attr(`ry`,3).attr(`x`,function(e){return e.milestone?b(e.startTime)+i+.5*(b(e.endTime)-b(e.startTime))-.5*s:b(e.startTime)+i}).attr(`y`,function(e,n){return n=e.order,e.vert?o.gridLineStartPadding:n*t+r}).attr(`width`,function(e){return e.milestone?s:e.vert?.08*s:b(e.renderEndTime||e.endTime)-b(e.startTime)}).attr(`height`,function(e){return e.vert?p.length*(o.barHeight+o.barGap)+o.barHeight*2:s}).attr(`transform-origin`,function(e,n){return n=e.order,(b(e.startTime)+i+.5*(b(e.endTime)-b(e.startTime))).toString()+`px `+(n*t+r+.5*s).toString()+`px`}).attr(`class`,function(e){let t=``;e.classes.length>0&&(t=e.classes.join(` `));let n=0;for(let[t,r]of m.entries())e.type===r&&(n=t%o.numberSectionStyles);let r=``;return e.active?e.crit?r+=` activeCrit`:r=` active`:e.done?r=e.crit?` doneCrit`:` done`:e.crit&&(r+=` crit`),r.length===0&&(r=` task`),e.milestone&&(r=` milestone `+r),e.vert&&(r=` vert `+r),r+=n,r+=` `+t,`task`+r}),d.append(`text`).attr(`id`,function(e){return n+`-`+e.id+`-text`}).text(function(e){return e.task}).attr(`font-size`,o.fontSize).attr(`x`,function(e){let t=b(e.startTime),n=b(e.renderEndTime||e.endTime);if(e.milestone&&(t+=.5*(b(e.endTime)-b(e.startTime))-.5*s,n=t+s),e.vert)return b(e.startTime)+i;let r=this.getBBox().width;return r>n-t?n+r+1.5*o.leftPadding>l?t+i-5:n+i+5:(n-t)/2+t+i}).attr(`y`,function(e,n){return e.vert?o.gridLineStartPadding+p.length*(o.barHeight+o.barGap)+60:(n=e.order,n*t+o.barHeight/2+(o.fontSize/2-2)+r)}).attr(`text-height`,s).attr(`class`,function(e){let t=b(e.startTime),n=b(e.endTime);e.milestone&&(n=t+s);let r=this.getBBox().width,i=``;e.classes.length>0&&(i=e.classes.join(` `));let a=0;for(let[t,n]of m.entries())e.type===n&&(a=t%o.numberSectionStyles);let c=``;return e.active&&(c=e.crit?`activeCritText`+a:`activeText`+a),e.done?c=e.crit?c+` doneCritText`+a:c+` doneText`+a:e.crit&&(c=c+` critText`+a),e.milestone&&(c+=` milestoneText`),e.vert&&(c+=` vertText`),r>n-t?n+r+1.5*o.leftPadding>l?i+` taskTextOutsideLeft taskTextOutside`+a+` `+c:i+` taskTextOutsideRight taskTextOutside`+a+` `+c+` width-`+r:i+` taskText taskText`+a+` `+c+` width-`+r}),x().securityLevel===`sandbox`){let e;e=f(`#i`+n);let t=e.nodes()[0].contentDocument;d.filter(function(e){return h.has(e.id)}).each(function(e){var r=t.querySelector(`#`+CSS.escape(n+`-`+e.id)),i=t.querySelector(`#`+CSS.escape(n+`-`+e.id+`-text`));let a=r.parentNode;var o=t.createElement(`a`);o.setAttribute(`xlink:href`,h.get(e.id)),o.setAttribute(`target`,`_top`),a.appendChild(o),o.appendChild(r),o.appendChild(i)})}}t(w,`drawRects`);function A(e,t,i,s,c,l,u,d){if(u.length===0&&d.length===0)return;let f,p;for(let{startTime:e,endTime:t}of l)(f===void 0||e<f)&&(f=e),(p===void 0||t>p)&&(p=t);if(!f||!p)return;if((0,R.default)(p).diff((0,R.default)(f),`year`)>5){r.warn(`The difference between the min and max time is more than 5 years. This will cause performance issues. Skipping drawing exclude days.`);return}let m=a.db.getDateFormat(),h=[],g=null,_=(0,R.default)(f);for(;_.valueOf()<=p;)a.db.isInvalidDate(_,m,u,d)?g?g.end=_:g={start:_,end:_}:g&&=(h.push(g),null),_=_.add(1,`d`);y.append(`g`).selectAll(`rect`).data(h).enter().append(`rect`).attr(`id`,e=>n+`-exclude-`+e.start.format(`YYYY-MM-DD`)).attr(`x`,e=>b(e.start.startOf(`day`))+i).attr(`y`,o.gridLineStartPadding).attr(`width`,e=>b(e.end.endOf(`day`))-b(e.start.startOf(`day`))).attr(`height`,c-t-o.gridLineStartPadding).attr(`transform-origin`,function(t,n){return(b(t.start)+i+.5*(b(t.end)-b(t.start))).toString()+`px `+(n*e+.5*c).toString()+`px`}).attr(`class`,`exclude-range`)}t(A,`drawExcludeDays`);function j(e,t,n,r){if(n<=0||e>t)return 1/0;let i=t-e,a=R.default.duration({[r??`day`]:n}).asMilliseconds();return a<=0?1/0:Math.ceil(i/a)}t(j,`getEstimatedTickCount`);function ne(e,t,n,i){let s=a.db.getDateFormat(),c=a.db.getAxisFormat(),l;l=c||(s===`D`?`%d`:o.axisFormat??`%Y-%m-%d`);let u=Se(b).tickSize(-i+t+o.gridLineStartPadding).tickFormat(te(l)),d=/^([1-9]\d*)(millisecond|second|minute|hour|day|week|month)$/.exec(a.db.getTickInterval()||o.tickInterval);if(d!==null){let e=parseInt(d[1],10);if(isNaN(e)||e<=0)r.warn(`Invalid tick interval value: "${d[1]}". Skipping custom tick interval.`);else{let t=d[2],n=a.db.getWeekday()||o.weekday,i=b.domain(),s=i[0],c=i[1],l=j(s,c,e,t);if(l>on)r.warn(`The tick interval "${e}${t}" would generate ${l} ticks, which exceeds the maximum allowed (${on}). This may indicate an invalid date or time range. Skipping custom tick interval.`);else switch(t){case`millisecond`:u.ticks(k.every(e));break;case`second`:u.ticks(ce.every(e));break;case`minute`:u.ticks(E.every(e));break;case`hour`:u.ticks(O.every(e));break;case`day`:u.ticks(le.every(e));break;case`week`:u.ticks(rn[n].every(e));break;case`month`:u.ticks(oe.every(e));break}}}if(y.append(`g`).attr(`class`,`grid`).attr(`transform`,`translate(`+e+`, `+(i-50)+`)`).call(u).selectAll(`text`).style(`text-anchor`,`middle`).attr(`fill`,`#000`).attr(`stroke`,`none`).attr(`font-size`,10).attr(`dy`,`1em`),a.db.topAxisEnabled()||o.topAxis){let n=xe(b).tickSize(-i+t+o.gridLineStartPadding).tickFormat(te(l));if(d!==null){let e=parseInt(d[1],10);if(isNaN(e)||e<=0)r.warn(`Invalid tick interval value: "${d[1]}". Skipping custom tick interval.`);else{let t=d[2],r=a.db.getWeekday()||o.weekday,i=b.domain(),s=i[0],c=i[1];if(j(s,c,e,t)<=on)switch(t){case`millisecond`:n.ticks(k.every(e));break;case`second`:n.ticks(ce.every(e));break;case`minute`:n.ticks(E.every(e));break;case`hour`:n.ticks(O.every(e));break;case`day`:n.ticks(le.every(e));break;case`week`:n.ticks(rn[r].every(e));break;case`month`:n.ticks(oe.every(e));break}}}y.append(`g`).attr(`class`,`grid`).attr(`transform`,`translate(`+e+`, `+t+`)`).call(n).selectAll(`text`).style(`text-anchor`,`middle`).attr(`fill`,`#000`).attr(`stroke`,`none`).attr(`font-size`,10)}}t(ne,`makeGrid`);function re(e,t){let n=0,r=Object.keys(h).map(e=>[e,h[e]]);y.append(`g`).selectAll(`text`).data(r).enter().append(function(e){let t=e[0].split(v.lineBreakRegex),n=-(t.length-1)/2,r=u.createElementNS(`http://www.w3.org/2000/svg`,`text`);r.setAttribute(`dy`,n+`em`);for(let[e,n]of t.entries()){let t=u.createElementNS(`http://www.w3.org/2000/svg`,`tspan`);t.setAttribute(`alignment-baseline`,`central`),t.setAttribute(`x`,`10`),e>0&&t.setAttribute(`dy`,`1em`),t.textContent=n,r.appendChild(t)}return r}).attr(`x`,10).attr(`y`,function(i,a){if(a>0)for(let o=0;o<a;o++)return n+=r[a-1][1],i[1]*e/2+n*e+t;else return i[1]*e/2+t}).attr(`font-size`,o.sectionFontSize).attr(`class`,function(e){for(let[t,n]of m.entries())if(e[0]===n)return`sectionTitle sectionTitle`+t%o.numberSectionStyles;return`sectionTitle`})}t(re,`vertLabels`);function ae(e,t,n,r){let i=a.db.getTodayMarker();if(i===`off`)return;let s=y.append(`g`).attr(`class`,`today`),c=new Date,l=s.append(`line`);l.attr(`x1`,b(c)+e).attr(`x2`,b(c)+e).attr(`y1`,o.titleTopMargin).attr(`y2`,r-o.titleTopMargin).attr(`class`,`today`),i!==``&&l.attr(`style`,i.replace(/,/g,`;`))}t(ae,`drawToday`);function se(e){let t={},n=[];for(let r=0,i=e.length;r<i;++r)Object.prototype.hasOwnProperty.call(t,e[r])||(t[e[r]]=!0,n.push(e[r]));return n}t(se,`checkUnique`)},`draw`)},styles:t(e=>`
  .mermaid-main-font {
        font-family: ${e.fontFamily};
  }

  .exclude-range {
    fill: ${e.excludeBkgColor};
  }

  .section {
    stroke: none;
    opacity: 0.2;
  }

  .section0 {
    fill: ${e.sectionBkgColor};
  }

  .section2 {
    fill: ${e.sectionBkgColor2};
  }

  .section1,
  .section3 {
    fill: ${e.altSectionBkgColor};
    opacity: 0.2;
  }

  .sectionTitle0 {
    fill: ${e.titleColor};
  }

  .sectionTitle1 {
    fill: ${e.titleColor};
  }

  .sectionTitle2 {
    fill: ${e.titleColor};
  }

  .sectionTitle3 {
    fill: ${e.titleColor};
  }

  .sectionTitle {
    text-anchor: start;
    font-family: ${e.fontFamily};
  }


  /* Grid and axis */

  .grid .tick {
    stroke: ${e.gridColor};
    opacity: 0.8;
    shape-rendering: crispEdges;
  }

  .grid .tick text {
    font-family: ${e.fontFamily};
    fill: ${e.textColor};
  }

  .grid path {
    stroke-width: 0;
  }


  /* Today line */

  .today {
    fill: none;
    stroke: ${e.todayLineColor};
    stroke-width: 2px;
  }


  /* Task styling */

  /* Default task */

  .task {
    stroke-width: 2;
  }

  .taskText {
    text-anchor: middle;
    font-family: ${e.fontFamily};
  }

  .taskTextOutsideRight {
    fill: ${e.taskTextDarkColor};
    text-anchor: start;
    font-family: ${e.fontFamily};
  }

  .taskTextOutsideLeft {
    fill: ${e.taskTextDarkColor};
    text-anchor: end;
  }


  /* Special case clickable */

  .task.clickable {
    cursor: pointer;
  }

  .taskText.clickable {
    cursor: pointer;
    fill: ${e.taskTextClickableColor} !important;
    font-weight: bold;
  }

  .taskTextOutsideLeft.clickable {
    cursor: pointer;
    fill: ${e.taskTextClickableColor} !important;
    font-weight: bold;
  }

  .taskTextOutsideRight.clickable {
    cursor: pointer;
    fill: ${e.taskTextClickableColor} !important;
    font-weight: bold;
  }


  /* Specific task settings for the sections*/

  .taskText0,
  .taskText1,
  .taskText2,
  .taskText3 {
    fill: ${e.taskTextColor};
  }

  .task0,
  .task1,
  .task2,
  .task3 {
    fill: ${e.taskBkgColor};
    stroke: ${e.taskBorderColor};
  }

  .taskTextOutside0,
  .taskTextOutside2
  {
    fill: ${e.taskTextOutsideColor};
  }

  .taskTextOutside1,
  .taskTextOutside3 {
    fill: ${e.taskTextOutsideColor};
  }


  /* Active task */

  .active0,
  .active1,
  .active2,
  .active3 {
    fill: ${e.activeTaskBkgColor};
    stroke: ${e.activeTaskBorderColor};
  }

  .activeText0,
  .activeText1,
  .activeText2,
  .activeText3 {
    fill: ${e.taskTextDarkColor} !important;
  }


  /* Completed task */

  .done0,
  .done1,
  .done2,
  .done3 {
    stroke: ${e.doneTaskBorderColor};
    fill: ${e.doneTaskBkgColor};
    stroke-width: 2;
  }

  .doneText0,
  .doneText1,
  .doneText2,
  .doneText3 {
    fill: ${e.taskTextDarkColor} !important;
  }

  /* Done task text displayed outside the bar sits against the diagram background,
     not against the done-task bar, so it must use the outside/contrast color. */
  .doneText0.taskTextOutsideLeft,
  .doneText0.taskTextOutsideRight,
  .doneText1.taskTextOutsideLeft,
  .doneText1.taskTextOutsideRight,
  .doneText2.taskTextOutsideLeft,
  .doneText2.taskTextOutsideRight,
  .doneText3.taskTextOutsideLeft,
  .doneText3.taskTextOutsideRight {
    fill: ${e.taskTextOutsideColor} !important;
  }


  /* Tasks on the critical line */

  .crit0,
  .crit1,
  .crit2,
  .crit3 {
    stroke: ${e.critBorderColor};
    fill: ${e.critBkgColor};
    stroke-width: 2;
  }

  .activeCrit0,
  .activeCrit1,
  .activeCrit2,
  .activeCrit3 {
    stroke: ${e.critBorderColor};
    fill: ${e.activeTaskBkgColor};
    stroke-width: 2;
  }

  .doneCrit0,
  .doneCrit1,
  .doneCrit2,
  .doneCrit3 {
    stroke: ${e.critBorderColor};
    fill: ${e.doneTaskBkgColor};
    stroke-width: 2;
    cursor: pointer;
    shape-rendering: crispEdges;
  }

  .milestone {
    transform: rotate(45deg) scale(0.8,0.8);
  }

  .milestoneText {
    font-style: italic;
  }
  .doneCritText0,
  .doneCritText1,
  .doneCritText2,
  .doneCritText3 {
    fill: ${e.taskTextDarkColor} !important;
  }

  /* Done-crit task text outside the bar \u2014 same reasoning as doneText above. */
  .doneCritText0.taskTextOutsideLeft,
  .doneCritText0.taskTextOutsideRight,
  .doneCritText1.taskTextOutsideLeft,
  .doneCritText1.taskTextOutsideRight,
  .doneCritText2.taskTextOutsideLeft,
  .doneCritText2.taskTextOutsideRight,
  .doneCritText3.taskTextOutsideLeft,
  .doneCritText3.taskTextOutsideRight {
    fill: ${e.taskTextOutsideColor} !important;
  }

  .vert {
    stroke: ${e.vertLineColor};
  }

  .vertText {
    font-size: 15px;
    text-anchor: middle;
    fill: ${e.vertLineColor} !important;
  }

  .activeCritText0,
  .activeCritText1,
  .activeCritText2,
  .activeCritText3 {
    fill: ${e.taskTextDarkColor} !important;
  }

  .titleText {
    text-anchor: middle;
    font-size: 18px;
    fill: ${e.titleColor||e.textColor};
    font-family: ${e.fontFamily};
  }
`,`getStyles`)};export{sn as diagram};