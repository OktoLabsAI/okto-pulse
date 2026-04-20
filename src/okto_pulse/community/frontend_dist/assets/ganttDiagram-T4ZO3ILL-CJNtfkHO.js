import{aT as me,aU as Ne,aV as ke,aW as ye,aX as ge,aY as Yt,aZ as Pe,aO as It,aP as $t,g as ze,s as Ve,q as Re,p as He,a as Be,b as Ge,_ as c,c as ft,d as Tt,a_ as Xe,a$ as je,b0 as qe,e as Ue,K as Ze,b1 as j,l as ot,b2 as ie,b3 as se,b4 as Ke,b5 as Qe,b6 as Je,b7 as tn,b8 as en,b9 as nn,ba as sn,bb as re,bc as ae,bd as oe,be as ce,bf as le,k as rn,j as an,z as on,u as cn}from"./index-KJt-la-z.js";function ln(t){return t}var _t=1,Lt=2,zt=3,wt=4,ue=1e-6;function un(t){return"translate("+t+",0)"}function dn(t){return"translate(0,"+t+")"}function fn(t){return e=>+t(e)}function hn(t,e){return e=Math.max(0,t.bandwidth()-e*2)/2,t.round()&&(e=Math.round(e)),s=>+t(s)+e}function mn(){return!this.__axis}function pe(t,e){var s=[],i=null,a=null,f=6,d=6,b=3,E=typeof window<"u"&&window.devicePixelRatio>1?0:.5,A=t===_t||t===wt?-1:1,T=t===wt||t===Lt?"x":"y",F=t===_t||t===zt?un:dn;function C(D){var V=i??(e.ticks?e.ticks.apply(e,s):e.domain()),I=a??(e.tickFormat?e.tickFormat.apply(e,s):ln),S=Math.max(f,0)+b,M=e.range(),W=+M[0]+E,L=+M[M.length-1]+E,R=(e.bandwidth?hn:fn)(e.copy(),E),H=D.selection?D.selection():D,$=H.selectAll(".domain").data([null]),p=H.selectAll(".tick").data(V,e).order(),h=p.exit(),u=p.enter().append("g").attr("class","tick"),x=p.select("line"),v=p.select("text");$=$.merge($.enter().insert("path",".tick").attr("class","domain").attr("stroke","currentColor")),p=p.merge(u),x=x.merge(u.append("line").attr("stroke","currentColor").attr(T+"2",A*f)),v=v.merge(u.append("text").attr("fill","currentColor").attr(T,A*S).attr("dy",t===_t?"0em":t===zt?"0.71em":"0.32em")),D!==H&&($=$.transition(D),p=p.transition(D),x=x.transition(D),v=v.transition(D),h=h.transition(D).attr("opacity",ue).attr("transform",function(k){return isFinite(k=R(k))?F(k+E):this.getAttribute("transform")}),u.attr("opacity",ue).attr("transform",function(k){var m=this.parentNode.__axis;return F((m&&isFinite(m=m(k))?m:R(k))+E)})),h.remove(),$.attr("d",t===wt||t===Lt?d?"M"+A*d+","+W+"H"+E+"V"+L+"H"+A*d:"M"+E+","+W+"V"+L:d?"M"+W+","+A*d+"V"+E+"H"+L+"V"+A*d:"M"+W+","+E+"H"+L),p.attr("opacity",1).attr("transform",function(k){return F(R(k)+E)}),x.attr(T+"2",A*f),v.attr(T,A*S).text(I),H.filter(mn).attr("fill","none").attr("font-size",10).attr("font-family","sans-serif").attr("text-anchor",t===Lt?"start":t===wt?"end":"middle"),H.each(function(){this.__axis=R})}return C.scale=function(D){return arguments.length?(e=D,C):e},C.ticks=function(){return s=Array.from(arguments),C},C.tickArguments=function(D){return arguments.length?(s=D==null?[]:Array.from(D),C):s.slice()},C.tickValues=function(D){return arguments.length?(i=D==null?null:Array.from(D),C):i&&i.slice()},C.tickFormat=function(D){return arguments.length?(a=D,C):a},C.tickSize=function(D){return arguments.length?(f=d=+D,C):f},C.tickSizeInner=function(D){return arguments.length?(f=+D,C):f},C.tickSizeOuter=function(D){return arguments.length?(d=+D,C):d},C.tickPadding=function(D){return arguments.length?(b=+D,C):b},C.offset=function(D){return arguments.length?(E=+D,C):E},C}function kn(t){return pe(_t,t)}function yn(t){return pe(zt,t)}const gn=Math.PI/180,pn=180/Math.PI,Ct=18,ve=.96422,xe=1,be=.82521,Te=4/29,ht=6/29,we=3*ht*ht,vn=ht*ht*ht;function _e(t){if(t instanceof et)return new et(t.l,t.a,t.b,t.opacity);if(t instanceof it)return De(t);t instanceof me||(t=Ne(t));var e=Nt(t.r),s=Nt(t.g),i=Nt(t.b),a=Ft((.2225045*e+.7168786*s+.0606169*i)/xe),f,d;return e===s&&s===i?f=d=a:(f=Ft((.4360747*e+.3850649*s+.1430804*i)/ve),d=Ft((.0139322*e+.0971045*s+.7141733*i)/be)),new et(116*a-16,500*(f-a),200*(a-d),t.opacity)}function xn(t,e,s,i){return arguments.length===1?_e(t):new et(t,e,s,i??1)}function et(t,e,s,i){this.l=+t,this.a=+e,this.b=+s,this.opacity=+i}ke(et,xn,ye(ge,{brighter(t){return new et(this.l+Ct*(t??1),this.a,this.b,this.opacity)},darker(t){return new et(this.l-Ct*(t??1),this.a,this.b,this.opacity)},rgb(){var t=(this.l+16)/116,e=isNaN(this.a)?t:t+this.a/500,s=isNaN(this.b)?t:t-this.b/200;return e=ve*Ot(e),t=xe*Ot(t),s=be*Ot(s),new me(Wt(3.1338561*e-1.6168667*t-.4906146*s),Wt(-.9787684*e+1.9161415*t+.033454*s),Wt(.0719453*e-.2289914*t+1.4052427*s),this.opacity)}}));function Ft(t){return t>vn?Math.pow(t,1/3):t/we+Te}function Ot(t){return t>ht?t*t*t:we*(t-Te)}function Wt(t){return 255*(t<=.0031308?12.92*t:1.055*Math.pow(t,1/2.4)-.055)}function Nt(t){return(t/=255)<=.04045?t/12.92:Math.pow((t+.055)/1.055,2.4)}function bn(t){if(t instanceof it)return new it(t.h,t.c,t.l,t.opacity);if(t instanceof et||(t=_e(t)),t.a===0&&t.b===0)return new it(NaN,0<t.l&&t.l<100?0:NaN,t.l,t.opacity);var e=Math.atan2(t.b,t.a)*pn;return new it(e<0?e+360:e,Math.sqrt(t.a*t.a+t.b*t.b),t.l,t.opacity)}function Vt(t,e,s,i){return arguments.length===1?bn(t):new it(t,e,s,i??1)}function it(t,e,s,i){this.h=+t,this.c=+e,this.l=+s,this.opacity=+i}function De(t){if(isNaN(t.h))return new et(t.l,0,0,t.opacity);var e=t.h*gn;return new et(t.l,Math.cos(e)*t.c,Math.sin(e)*t.c,t.opacity)}ke(it,Vt,ye(ge,{brighter(t){return new it(this.h,this.c,this.l+Ct*(t??1),this.opacity)},darker(t){return new it(this.h,this.c,this.l-Ct*(t??1),this.opacity)},rgb(){return De(this).rgb()}}));function Tn(t){return function(e,s){var i=t((e=Vt(e)).h,(s=Vt(s)).h),a=Yt(e.c,s.c),f=Yt(e.l,s.l),d=Yt(e.opacity,s.opacity);return function(b){return e.h=i(b),e.c=a(b),e.l=f(b),e.opacity=d(b),e+""}}}const wn=Tn(Pe);var Se={exports:{}};(function(t,e){(function(s,i){t.exports=i()})(It,function(){var s="day";return function(i,a,f){var d=function(A){return A.add(4-A.isoWeekday(),s)},b=a.prototype;b.isoWeekYear=function(){return d(this).year()},b.isoWeek=function(A){if(!this.$utils().u(A))return this.add(7*(A-this.isoWeek()),s);var T,F,C,D,V=d(this),I=(T=this.isoWeekYear(),F=this.$u,C=(F?f.utc:f)().year(T).startOf("year"),D=4-C.isoWeekday(),C.isoWeekday()>4&&(D+=7),C.add(D,s));return V.diff(I,"week")+1},b.isoWeekday=function(A){return this.$utils().u(A)?this.day()||7:this.day(this.day()%7?A:A-7)};var E=b.startOf;b.startOf=function(A,T){var F=this.$utils(),C=!!F.u(T)||T;return F.p(A)==="isoweek"?C?this.date(this.date()-(this.isoWeekday()-1)).startOf("day"):this.date(this.date()-1-(this.isoWeekday()-1)+7).endOf("day"):E.bind(this)(A,T)}}})})(Se);var _n=Se.exports;const Dn=$t(_n);var Ce={exports:{}};(function(t,e){(function(s,i){t.exports=i()})(It,function(){var s={LTS:"h:mm:ss A",LT:"h:mm A",L:"MM/DD/YYYY",LL:"MMMM D, YYYY",LLL:"MMMM D, YYYY h:mm A",LLLL:"dddd, MMMM D, YYYY h:mm A"},i=/(\[[^[]*\])|([-_:/.,()\s]+)|(A|a|Q|YYYY|YY?|ww?|MM?M?M?|Do|DD?|hh?|HH?|mm?|ss?|S{1,3}|z|ZZ?)/g,a=/\d/,f=/\d\d/,d=/\d\d?/,b=/\d*[^-_:/,()\s\d]+/,E={},A=function(S){return(S=+S)+(S>68?1900:2e3)},T=function(S){return function(M){this[S]=+M}},F=[/[+-]\d\d:?(\d\d)?|Z/,function(S){(this.zone||(this.zone={})).offset=function(M){if(!M||M==="Z")return 0;var W=M.match(/([+-]|\d\d)/g),L=60*W[1]+(+W[2]||0);return L===0?0:W[0]==="+"?-L:L}(S)}],C=function(S){var M=E[S];return M&&(M.indexOf?M:M.s.concat(M.f))},D=function(S,M){var W,L=E.meridiem;if(L){for(var R=1;R<=24;R+=1)if(S.indexOf(L(R,0,M))>-1){W=R>12;break}}else W=S===(M?"pm":"PM");return W},V={A:[b,function(S){this.afternoon=D(S,!1)}],a:[b,function(S){this.afternoon=D(S,!0)}],Q:[a,function(S){this.month=3*(S-1)+1}],S:[a,function(S){this.milliseconds=100*+S}],SS:[f,function(S){this.milliseconds=10*+S}],SSS:[/\d{3}/,function(S){this.milliseconds=+S}],s:[d,T("seconds")],ss:[d,T("seconds")],m:[d,T("minutes")],mm:[d,T("minutes")],H:[d,T("hours")],h:[d,T("hours")],HH:[d,T("hours")],hh:[d,T("hours")],D:[d,T("day")],DD:[f,T("day")],Do:[b,function(S){var M=E.ordinal,W=S.match(/\d+/);if(this.day=W[0],M)for(var L=1;L<=31;L+=1)M(L).replace(/\[|\]/g,"")===S&&(this.day=L)}],w:[d,T("week")],ww:[f,T("week")],M:[d,T("month")],MM:[f,T("month")],MMM:[b,function(S){var M=C("months"),W=(C("monthsShort")||M.map(function(L){return L.slice(0,3)})).indexOf(S)+1;if(W<1)throw new Error;this.month=W%12||W}],MMMM:[b,function(S){var M=C("months").indexOf(S)+1;if(M<1)throw new Error;this.month=M%12||M}],Y:[/[+-]?\d+/,T("year")],YY:[f,function(S){this.year=A(S)}],YYYY:[/\d{4}/,T("year")],Z:F,ZZ:F};function I(S){var M,W;M=S,W=E&&E.formats;for(var L=(S=M.replace(/(\[[^\]]+])|(LTS?|l{1,4}|L{1,4})/g,function(x,v,k){var m=k&&k.toUpperCase();return v||W[k]||s[k]||W[m].replace(/(\[[^\]]+])|(MMMM|MM|DD|dddd)/g,function(o,l,y){return l||y.slice(1)})})).match(i),R=L.length,H=0;H<R;H+=1){var $=L[H],p=V[$],h=p&&p[0],u=p&&p[1];L[H]=u?{regex:h,parser:u}:$.replace(/^\[|\]$/g,"")}return function(x){for(var v={},k=0,m=0;k<R;k+=1){var o=L[k];if(typeof o=="string")m+=o.length;else{var l=o.regex,y=o.parser,g=x.slice(m),w=l.exec(g)[0];y.call(v,w),x=x.replace(w,"")}}return function(r){var z=r.afternoon;if(z!==void 0){var n=r.hours;z?n<12&&(r.hours+=12):n===12&&(r.hours=0),delete r.afternoon}}(v),v}}return function(S,M,W){W.p.customParseFormat=!0,S&&S.parseTwoDigitYear&&(A=S.parseTwoDigitYear);var L=M.prototype,R=L.parse;L.parse=function(H){var $=H.date,p=H.utc,h=H.args;this.$u=p;var u=h[1];if(typeof u=="string"){var x=h[2]===!0,v=h[3]===!0,k=x||v,m=h[2];v&&(m=h[2]),E=this.$locale(),!x&&m&&(E=W.Ls[m]),this.$d=function(g,w,r,z){try{if(["x","X"].indexOf(w)>-1)return new Date((w==="X"?1e3:1)*g);var n=I(w)(g),_=n.year,P=n.month,N=n.day,O=n.hours,X=n.minutes,Y=n.seconds,K=n.milliseconds,st=n.zone,lt=n.week,yt=new Date,gt=N||(_||P?1:yt.getDate()),ut=_||yt.getFullYear(),B=0;_&&!P||(B=P>0?P-1:yt.getMonth());var Z,q=O||0,at=X||0,Q=Y||0,rt=K||0;return st?new Date(Date.UTC(ut,B,gt,q,at,Q,rt+60*st.offset*1e3)):r?new Date(Date.UTC(ut,B,gt,q,at,Q,rt)):(Z=new Date(ut,B,gt,q,at,Q,rt),lt&&(Z=z(Z).week(lt).toDate()),Z)}catch{return new Date("")}}($,u,p,W),this.init(),m&&m!==!0&&(this.$L=this.locale(m).$L),k&&$!=this.format(u)&&(this.$d=new Date("")),E={}}else if(u instanceof Array)for(var o=u.length,l=1;l<=o;l+=1){h[1]=u[l-1];var y=W.apply(this,h);if(y.isValid()){this.$d=y.$d,this.$L=y.$L,this.init();break}l===o&&(this.$d=new Date(""))}else R.call(this,H)}}})})(Ce);var Sn=Ce.exports;const Cn=$t(Sn);var Me={exports:{}};(function(t,e){(function(s,i){t.exports=i()})(It,function(){return function(s,i){var a=i.prototype,f=a.format;a.format=function(d){var b=this,E=this.$locale();if(!this.isValid())return f.bind(this)(d);var A=this.$utils(),T=(d||"YYYY-MM-DDTHH:mm:ssZ").replace(/\[([^\]]+)]|Q|wo|ww|w|WW|W|zzz|z|gggg|GGGG|Do|X|x|k{1,2}|S/g,function(F){switch(F){case"Q":return Math.ceil((b.$M+1)/3);case"Do":return E.ordinal(b.$D);case"gggg":return b.weekYear();case"GGGG":return b.isoWeekYear();case"wo":return E.ordinal(b.week(),"W");case"w":case"ww":return A.s(b.week(),F==="w"?1:2,"0");case"W":case"WW":return A.s(b.isoWeek(),F==="W"?1:2,"0");case"k":case"kk":return A.s(String(b.$H===0?24:b.$H),F==="k"?1:2,"0");case"X":return Math.floor(b.$d.getTime()/1e3);case"x":return b.$d.getTime();case"z":return"["+b.offsetName()+"]";case"zzz":return"["+b.offsetName("long")+"]";default:return F}});return f.bind(this)(T)}}})})(Me);var Mn=Me.exports;const En=$t(Mn);var Ee={exports:{}};(function(t,e){(function(s,i){t.exports=i()})(It,function(){var s,i,a=1e3,f=6e4,d=36e5,b=864e5,E=/\[([^\]]+)]|Y{1,4}|M{1,4}|D{1,2}|d{1,4}|H{1,2}|h{1,2}|a|A|m{1,2}|s{1,2}|Z{1,2}|SSS/g,A=31536e6,T=2628e6,F=/^(-|\+)?P(?:([-+]?[0-9,.]*)Y)?(?:([-+]?[0-9,.]*)M)?(?:([-+]?[0-9,.]*)W)?(?:([-+]?[0-9,.]*)D)?(?:T(?:([-+]?[0-9,.]*)H)?(?:([-+]?[0-9,.]*)M)?(?:([-+]?[0-9,.]*)S)?)?$/,C={years:A,months:T,days:b,hours:d,minutes:f,seconds:a,milliseconds:1,weeks:6048e5},D=function($){return $ instanceof R},V=function($,p,h){return new R($,h,p.$l)},I=function($){return i.p($)+"s"},S=function($){return $<0},M=function($){return S($)?Math.ceil($):Math.floor($)},W=function($){return Math.abs($)},L=function($,p){return $?S($)?{negative:!0,format:""+W($)+p}:{negative:!1,format:""+$+p}:{negative:!1,format:""}},R=function(){function $(h,u,x){var v=this;if(this.$d={},this.$l=x,h===void 0&&(this.$ms=0,this.parseFromMilliseconds()),u)return V(h*C[I(u)],this);if(typeof h=="number")return this.$ms=h,this.parseFromMilliseconds(),this;if(typeof h=="object")return Object.keys(h).forEach(function(o){v.$d[I(o)]=h[o]}),this.calMilliseconds(),this;if(typeof h=="string"){var k=h.match(F);if(k){var m=k.slice(2).map(function(o){return o!=null?Number(o):0});return this.$d.years=m[0],this.$d.months=m[1],this.$d.weeks=m[2],this.$d.days=m[3],this.$d.hours=m[4],this.$d.minutes=m[5],this.$d.seconds=m[6],this.calMilliseconds(),this}}return this}var p=$.prototype;return p.calMilliseconds=function(){var h=this;this.$ms=Object.keys(this.$d).reduce(function(u,x){return u+(h.$d[x]||0)*C[x]},0)},p.parseFromMilliseconds=function(){var h=this.$ms;this.$d.years=M(h/A),h%=A,this.$d.months=M(h/T),h%=T,this.$d.days=M(h/b),h%=b,this.$d.hours=M(h/d),h%=d,this.$d.minutes=M(h/f),h%=f,this.$d.seconds=M(h/a),h%=a,this.$d.milliseconds=h},p.toISOString=function(){var h=L(this.$d.years,"Y"),u=L(this.$d.months,"M"),x=+this.$d.days||0;this.$d.weeks&&(x+=7*this.$d.weeks);var v=L(x,"D"),k=L(this.$d.hours,"H"),m=L(this.$d.minutes,"M"),o=this.$d.seconds||0;this.$d.milliseconds&&(o+=this.$d.milliseconds/1e3,o=Math.round(1e3*o)/1e3);var l=L(o,"S"),y=h.negative||u.negative||v.negative||k.negative||m.negative||l.negative,g=k.format||m.format||l.format?"T":"",w=(y?"-":"")+"P"+h.format+u.format+v.format+g+k.format+m.format+l.format;return w==="P"||w==="-P"?"P0D":w},p.toJSON=function(){return this.toISOString()},p.format=function(h){var u=h||"YYYY-MM-DDTHH:mm:ss",x={Y:this.$d.years,YY:i.s(this.$d.years,2,"0"),YYYY:i.s(this.$d.years,4,"0"),M:this.$d.months,MM:i.s(this.$d.months,2,"0"),D:this.$d.days,DD:i.s(this.$d.days,2,"0"),H:this.$d.hours,HH:i.s(this.$d.hours,2,"0"),m:this.$d.minutes,mm:i.s(this.$d.minutes,2,"0"),s:this.$d.seconds,ss:i.s(this.$d.seconds,2,"0"),SSS:i.s(this.$d.milliseconds,3,"0")};return u.replace(E,function(v,k){return k||String(x[v])})},p.as=function(h){return this.$ms/C[I(h)]},p.get=function(h){var u=this.$ms,x=I(h);return x==="milliseconds"?u%=1e3:u=x==="weeks"?M(u/C[x]):this.$d[x],u||0},p.add=function(h,u,x){var v;return v=u?h*C[I(u)]:D(h)?h.$ms:V(h,this).$ms,V(this.$ms+v*(x?-1:1),this)},p.subtract=function(h,u){return this.add(h,u,!0)},p.locale=function(h){var u=this.clone();return u.$l=h,u},p.clone=function(){return V(this.$ms,this)},p.humanize=function(h){return s().add(this.$ms,"ms").locale(this.$l).fromNow(!h)},p.valueOf=function(){return this.asMilliseconds()},p.milliseconds=function(){return this.get("milliseconds")},p.asMilliseconds=function(){return this.as("milliseconds")},p.seconds=function(){return this.get("seconds")},p.asSeconds=function(){return this.as("seconds")},p.minutes=function(){return this.get("minutes")},p.asMinutes=function(){return this.as("minutes")},p.hours=function(){return this.get("hours")},p.asHours=function(){return this.as("hours")},p.days=function(){return this.get("days")},p.asDays=function(){return this.as("days")},p.weeks=function(){return this.get("weeks")},p.asWeeks=function(){return this.as("weeks")},p.months=function(){return this.get("months")},p.asMonths=function(){return this.as("months")},p.years=function(){return this.get("years")},p.asYears=function(){return this.as("years")},$}(),H=function($,p,h){return $.add(p.years()*h,"y").add(p.months()*h,"M").add(p.days()*h,"d").add(p.hours()*h,"h").add(p.minutes()*h,"m").add(p.seconds()*h,"s").add(p.milliseconds()*h,"ms")};return function($,p,h){s=h,i=h().$utils(),h.duration=function(v,k){var m=h.locale();return V(v,{$l:m},k)},h.isDuration=D;var u=p.prototype.add,x=p.prototype.subtract;p.prototype.add=function(v,k){return D(v)?H(this,v,1):u.bind(this)(v,k)},p.prototype.subtract=function(v,k){return D(v)?H(this,v,-1):x.bind(this)(v,k)}}})})(Ee);var In=Ee.exports;const $n=$t(In);var Rt=function(){var t=c(function(m,o,l,y){for(l=l||{},y=m.length;y--;l[m[y]]=o);return l},"o"),e=[6,8,10,12,13,14,15,16,17,18,20,21,22,23,24,25,26,27,28,29,30,31,33,35,36,38,40],s=[1,26],i=[1,27],a=[1,28],f=[1,29],d=[1,30],b=[1,31],E=[1,32],A=[1,33],T=[1,34],F=[1,9],C=[1,10],D=[1,11],V=[1,12],I=[1,13],S=[1,14],M=[1,15],W=[1,16],L=[1,19],R=[1,20],H=[1,21],$=[1,22],p=[1,23],h=[1,25],u=[1,35],x={trace:c(function(){},"trace"),yy:{},symbols_:{error:2,start:3,gantt:4,document:5,EOF:6,line:7,SPACE:8,statement:9,NL:10,weekday:11,weekday_monday:12,weekday_tuesday:13,weekday_wednesday:14,weekday_thursday:15,weekday_friday:16,weekday_saturday:17,weekday_sunday:18,weekend:19,weekend_friday:20,weekend_saturday:21,dateFormat:22,inclusiveEndDates:23,topAxis:24,axisFormat:25,tickInterval:26,excludes:27,includes:28,todayMarker:29,title:30,acc_title:31,acc_title_value:32,acc_descr:33,acc_descr_value:34,acc_descr_multiline_value:35,section:36,clickStatement:37,taskTxt:38,taskData:39,click:40,callbackname:41,callbackargs:42,href:43,clickStatementDebug:44,$accept:0,$end:1},terminals_:{2:"error",4:"gantt",6:"EOF",8:"SPACE",10:"NL",12:"weekday_monday",13:"weekday_tuesday",14:"weekday_wednesday",15:"weekday_thursday",16:"weekday_friday",17:"weekday_saturday",18:"weekday_sunday",20:"weekend_friday",21:"weekend_saturday",22:"dateFormat",23:"inclusiveEndDates",24:"topAxis",25:"axisFormat",26:"tickInterval",27:"excludes",28:"includes",29:"todayMarker",30:"title",31:"acc_title",32:"acc_title_value",33:"acc_descr",34:"acc_descr_value",35:"acc_descr_multiline_value",36:"section",38:"taskTxt",39:"taskData",40:"click",41:"callbackname",42:"callbackargs",43:"href"},productions_:[0,[3,3],[5,0],[5,2],[7,2],[7,1],[7,1],[7,1],[11,1],[11,1],[11,1],[11,1],[11,1],[11,1],[11,1],[19,1],[19,1],[9,1],[9,1],[9,1],[9,1],[9,1],[9,1],[9,1],[9,1],[9,1],[9,1],[9,1],[9,2],[9,2],[9,1],[9,1],[9,1],[9,2],[37,2],[37,3],[37,3],[37,4],[37,3],[37,4],[37,2],[44,2],[44,3],[44,3],[44,4],[44,3],[44,4],[44,2]],performAction:c(function(o,l,y,g,w,r,z){var n=r.length-1;switch(w){case 1:return r[n-1];case 2:this.$=[];break;case 3:r[n-1].push(r[n]),this.$=r[n-1];break;case 4:case 5:this.$=r[n];break;case 6:case 7:this.$=[];break;case 8:g.setWeekday("monday");break;case 9:g.setWeekday("tuesday");break;case 10:g.setWeekday("wednesday");break;case 11:g.setWeekday("thursday");break;case 12:g.setWeekday("friday");break;case 13:g.setWeekday("saturday");break;case 14:g.setWeekday("sunday");break;case 15:g.setWeekend("friday");break;case 16:g.setWeekend("saturday");break;case 17:g.setDateFormat(r[n].substr(11)),this.$=r[n].substr(11);break;case 18:g.enableInclusiveEndDates(),this.$=r[n].substr(18);break;case 19:g.TopAxis(),this.$=r[n].substr(8);break;case 20:g.setAxisFormat(r[n].substr(11)),this.$=r[n].substr(11);break;case 21:g.setTickInterval(r[n].substr(13)),this.$=r[n].substr(13);break;case 22:g.setExcludes(r[n].substr(9)),this.$=r[n].substr(9);break;case 23:g.setIncludes(r[n].substr(9)),this.$=r[n].substr(9);break;case 24:g.setTodayMarker(r[n].substr(12)),this.$=r[n].substr(12);break;case 27:g.setDiagramTitle(r[n].substr(6)),this.$=r[n].substr(6);break;case 28:this.$=r[n].trim(),g.setAccTitle(this.$);break;case 29:case 30:this.$=r[n].trim(),g.setAccDescription(this.$);break;case 31:g.addSection(r[n].substr(8)),this.$=r[n].substr(8);break;case 33:g.addTask(r[n-1],r[n]),this.$="task";break;case 34:this.$=r[n-1],g.setClickEvent(r[n-1],r[n],null);break;case 35:this.$=r[n-2],g.setClickEvent(r[n-2],r[n-1],r[n]);break;case 36:this.$=r[n-2],g.setClickEvent(r[n-2],r[n-1],null),g.setLink(r[n-2],r[n]);break;case 37:this.$=r[n-3],g.setClickEvent(r[n-3],r[n-2],r[n-1]),g.setLink(r[n-3],r[n]);break;case 38:this.$=r[n-2],g.setClickEvent(r[n-2],r[n],null),g.setLink(r[n-2],r[n-1]);break;case 39:this.$=r[n-3],g.setClickEvent(r[n-3],r[n-1],r[n]),g.setLink(r[n-3],r[n-2]);break;case 40:this.$=r[n-1],g.setLink(r[n-1],r[n]);break;case 41:case 47:this.$=r[n-1]+" "+r[n];break;case 42:case 43:case 45:this.$=r[n-2]+" "+r[n-1]+" "+r[n];break;case 44:case 46:this.$=r[n-3]+" "+r[n-2]+" "+r[n-1]+" "+r[n];break}},"anonymous"),table:[{3:1,4:[1,2]},{1:[3]},t(e,[2,2],{5:3}),{6:[1,4],7:5,8:[1,6],9:7,10:[1,8],11:17,12:s,13:i,14:a,15:f,16:d,17:b,18:E,19:18,20:A,21:T,22:F,23:C,24:D,25:V,26:I,27:S,28:M,29:W,30:L,31:R,33:H,35:$,36:p,37:24,38:h,40:u},t(e,[2,7],{1:[2,1]}),t(e,[2,3]),{9:36,11:17,12:s,13:i,14:a,15:f,16:d,17:b,18:E,19:18,20:A,21:T,22:F,23:C,24:D,25:V,26:I,27:S,28:M,29:W,30:L,31:R,33:H,35:$,36:p,37:24,38:h,40:u},t(e,[2,5]),t(e,[2,6]),t(e,[2,17]),t(e,[2,18]),t(e,[2,19]),t(e,[2,20]),t(e,[2,21]),t(e,[2,22]),t(e,[2,23]),t(e,[2,24]),t(e,[2,25]),t(e,[2,26]),t(e,[2,27]),{32:[1,37]},{34:[1,38]},t(e,[2,30]),t(e,[2,31]),t(e,[2,32]),{39:[1,39]},t(e,[2,8]),t(e,[2,9]),t(e,[2,10]),t(e,[2,11]),t(e,[2,12]),t(e,[2,13]),t(e,[2,14]),t(e,[2,15]),t(e,[2,16]),{41:[1,40],43:[1,41]},t(e,[2,4]),t(e,[2,28]),t(e,[2,29]),t(e,[2,33]),t(e,[2,34],{42:[1,42],43:[1,43]}),t(e,[2,40],{41:[1,44]}),t(e,[2,35],{43:[1,45]}),t(e,[2,36]),t(e,[2,38],{42:[1,46]}),t(e,[2,37]),t(e,[2,39])],defaultActions:{},parseError:c(function(o,l){if(l.recoverable)this.trace(o);else{var y=new Error(o);throw y.hash=l,y}},"parseError"),parse:c(function(o){var l=this,y=[0],g=[],w=[null],r=[],z=this.table,n="",_=0,P=0,N=2,O=1,X=r.slice.call(arguments,1),Y=Object.create(this.lexer),K={yy:{}};for(var st in this.yy)Object.prototype.hasOwnProperty.call(this.yy,st)&&(K.yy[st]=this.yy[st]);Y.setInput(o,K.yy),K.yy.lexer=Y,K.yy.parser=this,typeof Y.yylloc>"u"&&(Y.yylloc={});var lt=Y.yylloc;r.push(lt);var yt=Y.options&&Y.options.ranges;typeof K.yy.parseError=="function"?this.parseError=K.yy.parseError:this.parseError=Object.getPrototypeOf(this).parseError;function gt(U){y.length=y.length-2*U,w.length=w.length-U,r.length=r.length-U}c(gt,"popStack");function ut(){var U;return U=g.pop()||Y.lex()||O,typeof U!="number"&&(U instanceof Array&&(g=U,U=g.pop()),U=l.symbols_[U]||U),U}c(ut,"lex");for(var B,Z,q,at,Q={},rt,J,ne,bt;;){if(Z=y[y.length-1],this.defaultActions[Z]?q=this.defaultActions[Z]:((B===null||typeof B>"u")&&(B=ut()),q=z[Z]&&z[Z][B]),typeof q>"u"||!q.length||!q[0]){var At="";bt=[];for(rt in z[Z])this.terminals_[rt]&&rt>N&&bt.push("'"+this.terminals_[rt]+"'");Y.showPosition?At="Parse error on line "+(_+1)+`:
`+Y.showPosition()+`
Expecting `+bt.join(", ")+", got '"+(this.terminals_[B]||B)+"'":At="Parse error on line "+(_+1)+": Unexpected "+(B==O?"end of input":"'"+(this.terminals_[B]||B)+"'"),this.parseError(At,{text:Y.match,token:this.terminals_[B]||B,line:Y.yylineno,loc:lt,expected:bt})}if(q[0]instanceof Array&&q.length>1)throw new Error("Parse Error: multiple actions possible at state: "+Z+", token: "+B);switch(q[0]){case 1:y.push(B),w.push(Y.yytext),r.push(Y.yylloc),y.push(q[1]),B=null,P=Y.yyleng,n=Y.yytext,_=Y.yylineno,lt=Y.yylloc;break;case 2:if(J=this.productions_[q[1]][1],Q.$=w[w.length-J],Q._$={first_line:r[r.length-(J||1)].first_line,last_line:r[r.length-1].last_line,first_column:r[r.length-(J||1)].first_column,last_column:r[r.length-1].last_column},yt&&(Q._$.range=[r[r.length-(J||1)].range[0],r[r.length-1].range[1]]),at=this.performAction.apply(Q,[n,P,_,K.yy,q[1],w,r].concat(X)),typeof at<"u")return at;J&&(y=y.slice(0,-1*J*2),w=w.slice(0,-1*J),r=r.slice(0,-1*J)),y.push(this.productions_[q[1]][0]),w.push(Q.$),r.push(Q._$),ne=z[y[y.length-2]][y[y.length-1]],y.push(ne);break;case 3:return!0}}return!0},"parse")},v=function(){var m={EOF:1,parseError:c(function(l,y){if(this.yy.parser)this.yy.parser.parseError(l,y);else throw new Error(l)},"parseError"),setInput:c(function(o,l){return this.yy=l||this.yy||{},this._input=o,this._more=this._backtrack=this.done=!1,this.yylineno=this.yyleng=0,this.yytext=this.matched=this.match="",this.conditionStack=["INITIAL"],this.yylloc={first_line:1,first_column:0,last_line:1,last_column:0},this.options.ranges&&(this.yylloc.range=[0,0]),this.offset=0,this},"setInput"),input:c(function(){var o=this._input[0];this.yytext+=o,this.yyleng++,this.offset++,this.match+=o,this.matched+=o;var l=o.match(/(?:\r\n?|\n).*/g);return l?(this.yylineno++,this.yylloc.last_line++):this.yylloc.last_column++,this.options.ranges&&this.yylloc.range[1]++,this._input=this._input.slice(1),o},"input"),unput:c(function(o){var l=o.length,y=o.split(/(?:\r\n?|\n)/g);this._input=o+this._input,this.yytext=this.yytext.substr(0,this.yytext.length-l),this.offset-=l;var g=this.match.split(/(?:\r\n?|\n)/g);this.match=this.match.substr(0,this.match.length-1),this.matched=this.matched.substr(0,this.matched.length-1),y.length-1&&(this.yylineno-=y.length-1);var w=this.yylloc.range;return this.yylloc={first_line:this.yylloc.first_line,last_line:this.yylineno+1,first_column:this.yylloc.first_column,last_column:y?(y.length===g.length?this.yylloc.first_column:0)+g[g.length-y.length].length-y[0].length:this.yylloc.first_column-l},this.options.ranges&&(this.yylloc.range=[w[0],w[0]+this.yyleng-l]),this.yyleng=this.yytext.length,this},"unput"),more:c(function(){return this._more=!0,this},"more"),reject:c(function(){if(this.options.backtrack_lexer)this._backtrack=!0;else return this.parseError("Lexical error on line "+(this.yylineno+1)+`. You can only invoke reject() in the lexer when the lexer is of the backtracking persuasion (options.backtrack_lexer = true).
`+this.showPosition(),{text:"",token:null,line:this.yylineno});return this},"reject"),less:c(function(o){this.unput(this.match.slice(o))},"less"),pastInput:c(function(){var o=this.matched.substr(0,this.matched.length-this.match.length);return(o.length>20?"...":"")+o.substr(-20).replace(/\n/g,"")},"pastInput"),upcomingInput:c(function(){var o=this.match;return o.length<20&&(o+=this._input.substr(0,20-o.length)),(o.substr(0,20)+(o.length>20?"...":"")).replace(/\n/g,"")},"upcomingInput"),showPosition:c(function(){var o=this.pastInput(),l=new Array(o.length+1).join("-");return o+this.upcomingInput()+`
`+l+"^"},"showPosition"),test_match:c(function(o,l){var y,g,w;if(this.options.backtrack_lexer&&(w={yylineno:this.yylineno,yylloc:{first_line:this.yylloc.first_line,last_line:this.last_line,first_column:this.yylloc.first_column,last_column:this.yylloc.last_column},yytext:this.yytext,match:this.match,matches:this.matches,matched:this.matched,yyleng:this.yyleng,offset:this.offset,_more:this._more,_input:this._input,yy:this.yy,conditionStack:this.conditionStack.slice(0),done:this.done},this.options.ranges&&(w.yylloc.range=this.yylloc.range.slice(0))),g=o[0].match(/(?:\r\n?|\n).*/g),g&&(this.yylineno+=g.length),this.yylloc={first_line:this.yylloc.last_line,last_line:this.yylineno+1,first_column:this.yylloc.last_column,last_column:g?g[g.length-1].length-g[g.length-1].match(/\r?\n?/)[0].length:this.yylloc.last_column+o[0].length},this.yytext+=o[0],this.match+=o[0],this.matches=o,this.yyleng=this.yytext.length,this.options.ranges&&(this.yylloc.range=[this.offset,this.offset+=this.yyleng]),this._more=!1,this._backtrack=!1,this._input=this._input.slice(o[0].length),this.matched+=o[0],y=this.performAction.call(this,this.yy,this,l,this.conditionStack[this.conditionStack.length-1]),this.done&&this._input&&(this.done=!1),y)return y;if(this._backtrack){for(var r in w)this[r]=w[r];return!1}return!1},"test_match"),next:c(function(){if(this.done)return this.EOF;this._input||(this.done=!0);var o,l,y,g;this._more||(this.yytext="",this.match="");for(var w=this._currentRules(),r=0;r<w.length;r++)if(y=this._input.match(this.rules[w[r]]),y&&(!l||y[0].length>l[0].length)){if(l=y,g=r,this.options.backtrack_lexer){if(o=this.test_match(y,w[r]),o!==!1)return o;if(this._backtrack){l=!1;continue}else return!1}else if(!this.options.flex)break}return l?(o=this.test_match(l,w[g]),o!==!1?o:!1):this._input===""?this.EOF:this.parseError("Lexical error on line "+(this.yylineno+1)+`. Unrecognized text.
`+this.showPosition(),{text:"",token:null,line:this.yylineno})},"next"),lex:c(function(){var l=this.next();return l||this.lex()},"lex"),begin:c(function(l){this.conditionStack.push(l)},"begin"),popState:c(function(){var l=this.conditionStack.length-1;return l>0?this.conditionStack.pop():this.conditionStack[0]},"popState"),_currentRules:c(function(){return this.conditionStack.length&&this.conditionStack[this.conditionStack.length-1]?this.conditions[this.conditionStack[this.conditionStack.length-1]].rules:this.conditions.INITIAL.rules},"_currentRules"),topState:c(function(l){return l=this.conditionStack.length-1-Math.abs(l||0),l>=0?this.conditionStack[l]:"INITIAL"},"topState"),pushState:c(function(l){this.begin(l)},"pushState"),stateStackSize:c(function(){return this.conditionStack.length},"stateStackSize"),options:{"case-insensitive":!0},performAction:c(function(l,y,g,w){switch(g){case 0:return this.begin("open_directive"),"open_directive";case 1:return this.begin("acc_title"),31;case 2:return this.popState(),"acc_title_value";case 3:return this.begin("acc_descr"),33;case 4:return this.popState(),"acc_descr_value";case 5:this.begin("acc_descr_multiline");break;case 6:this.popState();break;case 7:return"acc_descr_multiline_value";case 8:break;case 9:break;case 10:break;case 11:return 10;case 12:break;case 13:break;case 14:this.begin("href");break;case 15:this.popState();break;case 16:return 43;case 17:this.begin("callbackname");break;case 18:this.popState();break;case 19:this.popState(),this.begin("callbackargs");break;case 20:return 41;case 21:this.popState();break;case 22:return 42;case 23:this.begin("click");break;case 24:this.popState();break;case 25:return 40;case 26:return 4;case 27:return 22;case 28:return 23;case 29:return 24;case 30:return 25;case 31:return 26;case 32:return 28;case 33:return 27;case 34:return 29;case 35:return 12;case 36:return 13;case 37:return 14;case 38:return 15;case 39:return 16;case 40:return 17;case 41:return 18;case 42:return 20;case 43:return 21;case 44:return"date";case 45:return 30;case 46:return"accDescription";case 47:return 36;case 48:return 38;case 49:return 39;case 50:return":";case 51:return 6;case 52:return"INVALID"}},"anonymous"),rules:[/^(?:%%\{)/i,/^(?:accTitle\s*:\s*)/i,/^(?:(?!\n||)*[^\n]*)/i,/^(?:accDescr\s*:\s*)/i,/^(?:(?!\n||)*[^\n]*)/i,/^(?:accDescr\s*\{\s*)/i,/^(?:[\}])/i,/^(?:[^\}]*)/i,/^(?:%%(?!\{)*[^\n]*)/i,/^(?:[^\}]%%*[^\n]*)/i,/^(?:%%*[^\n]*[\n]*)/i,/^(?:[\n]+)/i,/^(?:\s+)/i,/^(?:%[^\n]*)/i,/^(?:href[\s]+["])/i,/^(?:["])/i,/^(?:[^"]*)/i,/^(?:call[\s]+)/i,/^(?:\([\s]*\))/i,/^(?:\()/i,/^(?:[^(]*)/i,/^(?:\))/i,/^(?:[^)]*)/i,/^(?:click[\s]+)/i,/^(?:[\s\n])/i,/^(?:[^\s\n]*)/i,/^(?:gantt\b)/i,/^(?:dateFormat\s[^#\n;]+)/i,/^(?:inclusiveEndDates\b)/i,/^(?:topAxis\b)/i,/^(?:axisFormat\s[^#\n;]+)/i,/^(?:tickInterval\s[^#\n;]+)/i,/^(?:includes\s[^#\n;]+)/i,/^(?:excludes\s[^#\n;]+)/i,/^(?:todayMarker\s[^\n;]+)/i,/^(?:weekday\s+monday\b)/i,/^(?:weekday\s+tuesday\b)/i,/^(?:weekday\s+wednesday\b)/i,/^(?:weekday\s+thursday\b)/i,/^(?:weekday\s+friday\b)/i,/^(?:weekday\s+saturday\b)/i,/^(?:weekday\s+sunday\b)/i,/^(?:weekend\s+friday\b)/i,/^(?:weekend\s+saturday\b)/i,/^(?:\d\d\d\d-\d\d-\d\d\b)/i,/^(?:title\s[^\n]+)/i,/^(?:accDescription\s[^#\n;]+)/i,/^(?:section\s[^\n]+)/i,/^(?:[^:\n]+)/i,/^(?::[^#\n;]+)/i,/^(?::)/i,/^(?:$)/i,/^(?:.)/i],conditions:{acc_descr_multiline:{rules:[6,7],inclusive:!1},acc_descr:{rules:[4],inclusive:!1},acc_title:{rules:[2],inclusive:!1},callbackargs:{rules:[21,22],inclusive:!1},callbackname:{rules:[18,19,20],inclusive:!1},href:{rules:[15,16],inclusive:!1},click:{rules:[24,25],inclusive:!1},INITIAL:{rules:[0,1,3,5,8,9,10,11,12,13,14,17,23,26,27,28,29,30,31,32,33,34,35,36,37,38,39,40,41,42,43,44,45,46,47,48,49,50,51,52],inclusive:!0}}};return m}();x.lexer=v;function k(){this.yy={}}return c(k,"Parser"),k.prototype=x,x.Parser=k,new k}();Rt.parser=Rt;var An=Rt;j.extend(Dn);j.extend(Cn);j.extend(En);var de={friday:5,saturday:6},tt="",Xt="",jt=void 0,qt="",pt=[],vt=[],Ut=new Map,Zt=[],Mt=[],kt="",Kt="",Ie=["active","done","crit","milestone","vert"],Qt=[],dt="",xt=!1,Jt=!1,te="sunday",Et="saturday",Ht=0,Yn=c(function(){Zt=[],Mt=[],kt="",Qt=[],Dt=0,Gt=void 0,St=void 0,G=[],tt="",Xt="",Kt="",jt=void 0,qt="",pt=[],vt=[],xt=!1,Jt=!1,Ht=0,Ut=new Map,dt="",on(),te="sunday",Et="saturday"},"clear"),Ln=c(function(t){dt=t},"setDiagramId"),Fn=c(function(t){Xt=t},"setAxisFormat"),On=c(function(){return Xt},"getAxisFormat"),Wn=c(function(t){jt=t},"setTickInterval"),Nn=c(function(){return jt},"getTickInterval"),Pn=c(function(t){qt=t},"setTodayMarker"),zn=c(function(){return qt},"getTodayMarker"),Vn=c(function(t){tt=t},"setDateFormat"),Rn=c(function(){xt=!0},"enableInclusiveEndDates"),Hn=c(function(){return xt},"endDatesAreInclusive"),Bn=c(function(){Jt=!0},"enableTopAxis"),Gn=c(function(){return Jt},"topAxisEnabled"),Xn=c(function(t){Kt=t},"setDisplayMode"),jn=c(function(){return Kt},"getDisplayMode"),qn=c(function(){return tt},"getDateFormat"),Un=c(function(t){pt=t.toLowerCase().split(/[\s,]+/)},"setIncludes"),Zn=c(function(){return pt},"getIncludes"),Kn=c(function(t){vt=t.toLowerCase().split(/[\s,]+/)},"setExcludes"),Qn=c(function(){return vt},"getExcludes"),Jn=c(function(){return Ut},"getLinks"),ti=c(function(t){kt=t,Zt.push(t)},"addSection"),ei=c(function(){return Zt},"getSections"),ni=c(function(){let t=fe();const e=10;let s=0;for(;!t&&s<e;)t=fe(),s++;return Mt=G,Mt},"getTasks"),$e=c(function(t,e,s,i){const a=t.format(e.trim()),f=t.format("YYYY-MM-DD");return i.includes(a)||i.includes(f)?!1:s.includes("weekends")&&(t.isoWeekday()===de[Et]||t.isoWeekday()===de[Et]+1)||s.includes(t.format("dddd").toLowerCase())?!0:s.includes(a)||s.includes(f)},"isInvalidDate"),ii=c(function(t){te=t},"setWeekday"),si=c(function(){return te},"getWeekday"),ri=c(function(t){Et=t},"setWeekend"),Ae=c(function(t,e,s,i){if(!s.length||t.manualEndTime)return;let a;t.startTime instanceof Date?a=j(t.startTime):a=j(t.startTime,e,!0),a=a.add(1,"d");let f;t.endTime instanceof Date?f=j(t.endTime):f=j(t.endTime,e,!0);const[d,b]=ai(a,f,e,s,i);t.endTime=d.toDate(),t.renderEndTime=b},"checkTaskDates"),ai=c(function(t,e,s,i,a){let f=!1,d=null;for(;t<=e;)f||(d=e.toDate()),f=$e(t,s,i,a),f&&(e=e.add(1,"d")),t=t.add(1,"d");return[e,d]},"fixTaskDates"),Bt=c(function(t,e,s){if(s=s.trim(),c(b=>{const E=b.trim();return E==="x"||E==="X"},"isTimestampFormat")(e)&&/^\d+$/.test(s))return new Date(Number(s));const f=/^after\s+(?<ids>[\d\w- ]+)/.exec(s);if(f!==null){let b=null;for(const A of f.groups.ids.split(" ")){let T=ct(A);T!==void 0&&(!b||T.endTime>b.endTime)&&(b=T)}if(b)return b.endTime;const E=new Date;return E.setHours(0,0,0,0),E}let d=j(s,e.trim(),!0);if(d.isValid())return d.toDate();{ot.debug("Invalid date:"+s),ot.debug("With date format:"+e.trim());const b=new Date(s);if(b===void 0||isNaN(b.getTime())||b.getFullYear()<-1e4||b.getFullYear()>1e4)throw new Error("Invalid date:"+s);return b}},"getStartDate"),Ye=c(function(t){const e=/^(\d+(?:\.\d+)?)([Mdhmswy]|ms)$/.exec(t.trim());return e!==null?[Number.parseFloat(e[1]),e[2]]:[NaN,"ms"]},"parseDuration"),Le=c(function(t,e,s,i=!1){s=s.trim();const f=/^until\s+(?<ids>[\d\w- ]+)/.exec(s);if(f!==null){let T=null;for(const C of f.groups.ids.split(" ")){let D=ct(C);D!==void 0&&(!T||D.startTime<T.startTime)&&(T=D)}if(T)return T.startTime;const F=new Date;return F.setHours(0,0,0,0),F}let d=j(s,e.trim(),!0);if(d.isValid())return i&&(d=d.add(1,"d")),d.toDate();let b=j(t);const[E,A]=Ye(s);if(!Number.isNaN(E)){const T=b.add(E,A);T.isValid()&&(b=T)}return b.toDate()},"getEndDate"),Dt=0,mt=c(function(t){return t===void 0?(Dt=Dt+1,"task"+Dt):t},"parseId"),oi=c(function(t,e){let s;e.substr(0,1)===":"?s=e.substr(1,e.length):s=e;const i=s.split(","),a={};ee(i,a,Ie);for(let d=0;d<i.length;d++)i[d]=i[d].trim();let f="";switch(i.length){case 1:a.id=mt(),a.startTime=t.endTime,f=i[0];break;case 2:a.id=mt(),a.startTime=Bt(void 0,tt,i[0]),f=i[1];break;case 3:a.id=mt(i[0]),a.startTime=Bt(void 0,tt,i[1]),f=i[2];break}return f&&(a.endTime=Le(a.startTime,tt,f,xt),a.manualEndTime=j(f,"YYYY-MM-DD",!0).isValid(),Ae(a,tt,vt,pt)),a},"compileData"),ci=c(function(t,e){let s;e.substr(0,1)===":"?s=e.substr(1,e.length):s=e;const i=s.split(","),a={};ee(i,a,Ie);for(let f=0;f<i.length;f++)i[f]=i[f].trim();switch(i.length){case 1:a.id=mt(),a.startTime={type:"prevTaskEnd",id:t},a.endTime={data:i[0]};break;case 2:a.id=mt(),a.startTime={type:"getStartDate",startData:i[0]},a.endTime={data:i[1]};break;case 3:a.id=mt(i[0]),a.startTime={type:"getStartDate",startData:i[1]},a.endTime={data:i[2]};break}return a},"parseData"),Gt,St,G=[],Fe={},li=c(function(t,e){const s={section:kt,type:kt,processed:!1,manualEndTime:!1,renderEndTime:null,raw:{data:e},task:t,classes:[]},i=ci(St,e);s.raw.startTime=i.startTime,s.raw.endTime=i.endTime,s.id=i.id,s.prevTaskId=St,s.active=i.active,s.done=i.done,s.crit=i.crit,s.milestone=i.milestone,s.vert=i.vert,s.order=Ht,Ht++;const a=G.push(s);St=s.id,Fe[s.id]=a-1},"addTask"),ct=c(function(t){const e=Fe[t];return G[e]},"findTaskById"),ui=c(function(t,e){const s={section:kt,type:kt,description:t,task:t,classes:[]},i=oi(Gt,e);s.startTime=i.startTime,s.endTime=i.endTime,s.id=i.id,s.active=i.active,s.done=i.done,s.crit=i.crit,s.milestone=i.milestone,s.vert=i.vert,Gt=s,Mt.push(s)},"addTaskOrg"),fe=c(function(){const t=c(function(s){const i=G[s];let a="";switch(G[s].raw.startTime.type){case"prevTaskEnd":{const f=ct(i.prevTaskId);i.startTime=f.endTime;break}case"getStartDate":a=Bt(void 0,tt,G[s].raw.startTime.startData),a&&(G[s].startTime=a);break}return G[s].startTime&&(G[s].endTime=Le(G[s].startTime,tt,G[s].raw.endTime.data,xt),G[s].endTime&&(G[s].processed=!0,G[s].manualEndTime=j(G[s].raw.endTime.data,"YYYY-MM-DD",!0).isValid(),Ae(G[s],tt,vt,pt))),G[s].processed},"compileTask");let e=!0;for(const[s,i]of G.entries())t(s),e=e&&i.processed;return e},"compileTasks"),di=c(function(t,e){let s=e;ft().securityLevel!=="loose"&&(s=an(e)),t.split(",").forEach(function(i){ct(i)!==void 0&&(We(i,()=>{window.open(s,"_self")}),Ut.set(i,s))}),Oe(t,"clickable")},"setLink"),Oe=c(function(t,e){t.split(",").forEach(function(s){let i=ct(s);i!==void 0&&i.classes.push(e)})},"setClass"),fi=c(function(t,e,s){if(ft().securityLevel!=="loose"||e===void 0)return;let i=[];if(typeof s=="string"){i=s.split(/,(?=(?:(?:[^"]*"){2})*[^"]*$)/);for(let f=0;f<i.length;f++){let d=i[f].trim();d.startsWith('"')&&d.endsWith('"')&&(d=d.substr(1,d.length-2)),i[f]=d}}i.length===0&&i.push(t),ct(t)!==void 0&&We(t,()=>{cn.runFunc(e,...i)})},"setClickFun"),We=c(function(t,e){Qt.push(function(){const s=dt?`${dt}-${t}`:t,i=document.querySelector(`[id="${s}"]`);i!==null&&i.addEventListener("click",function(){e()})},function(){const s=dt?`${dt}-${t}`:t,i=document.querySelector(`[id="${s}-text"]`);i!==null&&i.addEventListener("click",function(){e()})})},"pushFun"),hi=c(function(t,e,s){t.split(",").forEach(function(i){fi(i,e,s)}),Oe(t,"clickable")},"setClickEvent"),mi=c(function(t){Qt.forEach(function(e){e(t)})},"bindFunctions"),ki={getConfig:c(()=>ft().gantt,"getConfig"),clear:Yn,setDateFormat:Vn,getDateFormat:qn,enableInclusiveEndDates:Rn,endDatesAreInclusive:Hn,enableTopAxis:Bn,topAxisEnabled:Gn,setAxisFormat:Fn,getAxisFormat:On,setTickInterval:Wn,getTickInterval:Nn,setTodayMarker:Pn,getTodayMarker:zn,setAccTitle:Ge,getAccTitle:Be,setDiagramTitle:He,getDiagramTitle:Re,setDiagramId:Ln,setDisplayMode:Xn,getDisplayMode:jn,setAccDescription:Ve,getAccDescription:ze,addSection:ti,getSections:ei,getTasks:ni,addTask:li,findTaskById:ct,addTaskOrg:ui,setIncludes:Un,getIncludes:Zn,setExcludes:Kn,getExcludes:Qn,setClickEvent:hi,setLink:di,getLinks:Jn,bindFunctions:mi,parseDuration:Ye,isInvalidDate:$e,setWeekday:ii,getWeekday:si,setWeekend:ri};function ee(t,e,s){let i=!0;for(;i;)i=!1,s.forEach(function(a){const f="^\\s*"+a+"\\s*$",d=new RegExp(f);t[0].match(d)&&(e[a]=!0,t.shift(1),i=!0)})}c(ee,"getTaskTags");j.extend($n);var yi=c(function(){ot.debug("Something is calling, setConf, remove the call")},"setConf"),he={monday:sn,tuesday:nn,wednesday:en,thursday:tn,friday:Je,saturday:Qe,sunday:Ke},gi=c((t,e)=>{let s=[...t].map(()=>-1/0),i=[...t].sort((f,d)=>f.startTime-d.startTime||f.order-d.order),a=0;for(const f of i)for(let d=0;d<s.length;d++)if(f.startTime>=s[d]){s[d]=f.endTime,f.order=d+e,d>a&&(a=d);break}return a},"getMaxIntersections"),nt,Pt=1e4,pi=c(function(t,e,s,i){const a=ft().gantt;i.db.setDiagramId(e);const f=ft().securityLevel;let d;f==="sandbox"&&(d=Tt("#i"+e));const b=f==="sandbox"?Tt(d.nodes()[0].contentDocument.body):Tt("body"),E=f==="sandbox"?d.nodes()[0].contentDocument:document,A=E.getElementById(e);nt=A.parentElement.offsetWidth,nt===void 0&&(nt=1200),a.useWidth!==void 0&&(nt=a.useWidth);const T=i.db.getTasks();let F=[];for(const u of T)F.push(u.type);F=h(F);const C={};let D=2*a.topPadding;if(i.db.getDisplayMode()==="compact"||a.displayMode==="compact"){const u={};for(const v of T)u[v.section]===void 0?u[v.section]=[v]:u[v.section].push(v);let x=0;for(const v of Object.keys(u)){const k=gi(u[v],x)+1;x+=k,D+=k*(a.barHeight+a.barGap),C[v]=k}}else{D+=T.length*(a.barHeight+a.barGap);for(const u of F)C[u]=T.filter(x=>x.type===u).length}A.setAttribute("viewBox","0 0 "+nt+" "+D);const V=b.select(`[id="${e}"]`),I=Xe().domain([je(T,function(u){return u.startTime}),qe(T,function(u){return u.endTime})]).rangeRound([0,nt-a.leftPadding-a.rightPadding]);function S(u,x){const v=u.startTime,k=x.startTime;let m=0;return v>k?m=1:v<k&&(m=-1),m}c(S,"taskCompare"),T.sort(S),M(T,nt,D),Ue(V,D,nt,a.useMaxWidth),V.append("text").text(i.db.getDiagramTitle()).attr("x",nt/2).attr("y",a.titleTopMargin).attr("class","titleText");function M(u,x,v){const k=a.barHeight,m=k+a.barGap,o=a.topPadding,l=a.leftPadding,y=Ze().domain([0,F.length]).range(["#00B9FA","#F95002"]).interpolate(wn);L(m,o,l,x,v,u,i.db.getExcludes(),i.db.getIncludes()),H(l,o,x,v),W(u,m,o,l,k,y,x),$(m,o),p(l,o,x,v)}c(M,"makeGantt");function W(u,x,v,k,m,o,l){u.sort((n,_)=>n.vert===_.vert?0:n.vert?1:-1);const g=[...new Set(u.map(n=>n.order))].map(n=>u.find(_=>_.order===n));V.append("g").selectAll("rect").data(g).enter().append("rect").attr("x",0).attr("y",function(n,_){return _=n.order,_*x+v-2}).attr("width",function(){return l-a.rightPadding/2}).attr("height",x).attr("class",function(n){for(const[_,P]of F.entries())if(n.type===P)return"section section"+_%a.numberSectionStyles;return"section section0"}).enter();const w=V.append("g").selectAll("rect").data(u).enter(),r=i.db.getLinks();if(w.append("rect").attr("id",function(n){return e+"-"+n.id}).attr("rx",3).attr("ry",3).attr("x",function(n){return n.milestone?I(n.startTime)+k+.5*(I(n.endTime)-I(n.startTime))-.5*m:I(n.startTime)+k}).attr("y",function(n,_){return _=n.order,n.vert?a.gridLineStartPadding:_*x+v}).attr("width",function(n){return n.milestone?m:n.vert?.08*m:I(n.renderEndTime||n.endTime)-I(n.startTime)}).attr("height",function(n){return n.vert?T.length*(a.barHeight+a.barGap)+a.barHeight*2:m}).attr("transform-origin",function(n,_){return _=n.order,(I(n.startTime)+k+.5*(I(n.endTime)-I(n.startTime))).toString()+"px "+(_*x+v+.5*m).toString()+"px"}).attr("class",function(n){const _="task";let P="";n.classes.length>0&&(P=n.classes.join(" "));let N=0;for(const[X,Y]of F.entries())n.type===Y&&(N=X%a.numberSectionStyles);let O="";return n.active?n.crit?O+=" activeCrit":O=" active":n.done?n.crit?O=" doneCrit":O=" done":n.crit&&(O+=" crit"),O.length===0&&(O=" task"),n.milestone&&(O=" milestone "+O),n.vert&&(O=" vert "+O),O+=N,O+=" "+P,_+O}),w.append("text").attr("id",function(n){return e+"-"+n.id+"-text"}).text(function(n){return n.task}).attr("font-size",a.fontSize).attr("x",function(n){let _=I(n.startTime),P=I(n.renderEndTime||n.endTime);if(n.milestone&&(_+=.5*(I(n.endTime)-I(n.startTime))-.5*m,P=_+m),n.vert)return I(n.startTime)+k;const N=this.getBBox().width;return N>P-_?P+N+1.5*a.leftPadding>l?_+k-5:P+k+5:(P-_)/2+_+k}).attr("y",function(n,_){return n.vert?a.gridLineStartPadding+T.length*(a.barHeight+a.barGap)+60:(_=n.order,_*x+a.barHeight/2+(a.fontSize/2-2)+v)}).attr("text-height",m).attr("class",function(n){const _=I(n.startTime);let P=I(n.endTime);n.milestone&&(P=_+m);const N=this.getBBox().width;let O="";n.classes.length>0&&(O=n.classes.join(" "));let X=0;for(const[K,st]of F.entries())n.type===st&&(X=K%a.numberSectionStyles);let Y="";return n.active&&(n.crit?Y="activeCritText"+X:Y="activeText"+X),n.done?n.crit?Y=Y+" doneCritText"+X:Y=Y+" doneText"+X:n.crit&&(Y=Y+" critText"+X),n.milestone&&(Y+=" milestoneText"),n.vert&&(Y+=" vertText"),N>P-_?P+N+1.5*a.leftPadding>l?O+" taskTextOutsideLeft taskTextOutside"+X+" "+Y:O+" taskTextOutsideRight taskTextOutside"+X+" "+Y+" width-"+N:O+" taskText taskText"+X+" "+Y+" width-"+N}),ft().securityLevel==="sandbox"){let n;n=Tt("#i"+e);const _=n.nodes()[0].contentDocument;w.filter(function(P){return r.has(P.id)}).each(function(P){var N=_.querySelector("#"+CSS.escape(e+"-"+P.id)),O=_.querySelector("#"+CSS.escape(e+"-"+P.id+"-text"));const X=N.parentNode;var Y=_.createElement("a");Y.setAttribute("xlink:href",r.get(P.id)),Y.setAttribute("target","_top"),X.appendChild(Y),Y.appendChild(N),Y.appendChild(O)})}}c(W,"drawRects");function L(u,x,v,k,m,o,l,y){if(l.length===0&&y.length===0)return;let g,w;for(const{startTime:N,endTime:O}of o)(g===void 0||N<g)&&(g=N),(w===void 0||O>w)&&(w=O);if(!g||!w)return;if(j(w).diff(j(g),"year")>5){ot.warn("The difference between the min and max time is more than 5 years. This will cause performance issues. Skipping drawing exclude days.");return}const r=i.db.getDateFormat(),z=[];let n=null,_=j(g);for(;_.valueOf()<=w;)i.db.isInvalidDate(_,r,l,y)?n?n.end=_:n={start:_,end:_}:n&&(z.push(n),n=null),_=_.add(1,"d");V.append("g").selectAll("rect").data(z).enter().append("rect").attr("id",N=>e+"-exclude-"+N.start.format("YYYY-MM-DD")).attr("x",N=>I(N.start.startOf("day"))+v).attr("y",a.gridLineStartPadding).attr("width",N=>I(N.end.endOf("day"))-I(N.start.startOf("day"))).attr("height",m-x-a.gridLineStartPadding).attr("transform-origin",function(N,O){return(I(N.start)+v+.5*(I(N.end)-I(N.start))).toString()+"px "+(O*u+.5*m).toString()+"px"}).attr("class","exclude-range")}c(L,"drawExcludeDays");function R(u,x,v,k){if(v<=0||u>x)return 1/0;const m=x-u,o=j.duration({[k??"day"]:v}).asMilliseconds();return o<=0?1/0:Math.ceil(m/o)}c(R,"getEstimatedTickCount");function H(u,x,v,k){const m=i.db.getDateFormat(),o=i.db.getAxisFormat();let l;o?l=o:m==="D"?l="%d":l=a.axisFormat??"%Y-%m-%d";let y=yn(I).tickSize(-k+x+a.gridLineStartPadding).tickFormat(ie(l));const w=/^([1-9]\d*)(millisecond|second|minute|hour|day|week|month)$/.exec(i.db.getTickInterval()||a.tickInterval);if(w!==null){const r=parseInt(w[1],10);if(isNaN(r)||r<=0)ot.warn(`Invalid tick interval value: "${w[1]}". Skipping custom tick interval.`);else{const z=w[2],n=i.db.getWeekday()||a.weekday,_=I.domain(),P=_[0],N=_[1],O=R(P,N,r,z);if(O>Pt)ot.warn(`The tick interval "${r}${z}" would generate ${O} ticks, which exceeds the maximum allowed (${Pt}). This may indicate an invalid date or time range. Skipping custom tick interval.`);else switch(z){case"millisecond":y.ticks(le.every(r));break;case"second":y.ticks(ce.every(r));break;case"minute":y.ticks(oe.every(r));break;case"hour":y.ticks(ae.every(r));break;case"day":y.ticks(re.every(r));break;case"week":y.ticks(he[n].every(r));break;case"month":y.ticks(se.every(r));break}}}if(V.append("g").attr("class","grid").attr("transform","translate("+u+", "+(k-50)+")").call(y).selectAll("text").style("text-anchor","middle").attr("fill","#000").attr("stroke","none").attr("font-size",10).attr("dy","1em"),i.db.topAxisEnabled()||a.topAxis){let r=kn(I).tickSize(-k+x+a.gridLineStartPadding).tickFormat(ie(l));if(w!==null){const z=parseInt(w[1],10);if(isNaN(z)||z<=0)ot.warn(`Invalid tick interval value: "${w[1]}". Skipping custom tick interval.`);else{const n=w[2],_=i.db.getWeekday()||a.weekday,P=I.domain(),N=P[0],O=P[1];if(R(N,O,z,n)<=Pt)switch(n){case"millisecond":r.ticks(le.every(z));break;case"second":r.ticks(ce.every(z));break;case"minute":r.ticks(oe.every(z));break;case"hour":r.ticks(ae.every(z));break;case"day":r.ticks(re.every(z));break;case"week":r.ticks(he[_].every(z));break;case"month":r.ticks(se.every(z));break}}}V.append("g").attr("class","grid").attr("transform","translate("+u+", "+x+")").call(r).selectAll("text").style("text-anchor","middle").attr("fill","#000").attr("stroke","none").attr("font-size",10)}}c(H,"makeGrid");function $(u,x){let v=0;const k=Object.keys(C).map(m=>[m,C[m]]);V.append("g").selectAll("text").data(k).enter().append(function(m){const o=m[0].split(rn.lineBreakRegex),l=-(o.length-1)/2,y=E.createElementNS("http://www.w3.org/2000/svg","text");y.setAttribute("dy",l+"em");for(const[g,w]of o.entries()){const r=E.createElementNS("http://www.w3.org/2000/svg","tspan");r.setAttribute("alignment-baseline","central"),r.setAttribute("x","10"),g>0&&r.setAttribute("dy","1em"),r.textContent=w,y.appendChild(r)}return y}).attr("x",10).attr("y",function(m,o){if(o>0)for(let l=0;l<o;l++)return v+=k[o-1][1],m[1]*u/2+v*u+x;else return m[1]*u/2+x}).attr("font-size",a.sectionFontSize).attr("class",function(m){for(const[o,l]of F.entries())if(m[0]===l)return"sectionTitle sectionTitle"+o%a.numberSectionStyles;return"sectionTitle"})}c($,"vertLabels");function p(u,x,v,k){const m=i.db.getTodayMarker();if(m==="off")return;const o=V.append("g").attr("class","today"),l=new Date,y=o.append("line");y.attr("x1",I(l)+u).attr("x2",I(l)+u).attr("y1",a.titleTopMargin).attr("y2",k-a.titleTopMargin).attr("class","today"),m!==""&&y.attr("style",m.replace(/,/g,";"))}c(p,"drawToday");function h(u){const x={},v=[];for(let k=0,m=u.length;k<m;++k)Object.prototype.hasOwnProperty.call(x,u[k])||(x[u[k]]=!0,v.push(u[k]));return v}c(h,"checkUnique")},"draw"),vi={setConf:yi,draw:pi},xi=c(t=>`
  .mermaid-main-font {
        font-family: ${t.fontFamily};
  }

  .exclude-range {
    fill: ${t.excludeBkgColor};
  }

  .section {
    stroke: none;
    opacity: 0.2;
  }

  .section0 {
    fill: ${t.sectionBkgColor};
  }

  .section2 {
    fill: ${t.sectionBkgColor2};
  }

  .section1,
  .section3 {
    fill: ${t.altSectionBkgColor};
    opacity: 0.2;
  }

  .sectionTitle0 {
    fill: ${t.titleColor};
  }

  .sectionTitle1 {
    fill: ${t.titleColor};
  }

  .sectionTitle2 {
    fill: ${t.titleColor};
  }

  .sectionTitle3 {
    fill: ${t.titleColor};
  }

  .sectionTitle {
    text-anchor: start;
    font-family: ${t.fontFamily};
  }


  /* Grid and axis */

  .grid .tick {
    stroke: ${t.gridColor};
    opacity: 0.8;
    shape-rendering: crispEdges;
  }

  .grid .tick text {
    font-family: ${t.fontFamily};
    fill: ${t.textColor};
  }

  .grid path {
    stroke-width: 0;
  }


  /* Today line */

  .today {
    fill: none;
    stroke: ${t.todayLineColor};
    stroke-width: 2px;
  }


  /* Task styling */

  /* Default task */

  .task {
    stroke-width: 2;
  }

  .taskText {
    text-anchor: middle;
    font-family: ${t.fontFamily};
  }

  .taskTextOutsideRight {
    fill: ${t.taskTextDarkColor};
    text-anchor: start;
    font-family: ${t.fontFamily};
  }

  .taskTextOutsideLeft {
    fill: ${t.taskTextDarkColor};
    text-anchor: end;
  }


  /* Special case clickable */

  .task.clickable {
    cursor: pointer;
  }

  .taskText.clickable {
    cursor: pointer;
    fill: ${t.taskTextClickableColor} !important;
    font-weight: bold;
  }

  .taskTextOutsideLeft.clickable {
    cursor: pointer;
    fill: ${t.taskTextClickableColor} !important;
    font-weight: bold;
  }

  .taskTextOutsideRight.clickable {
    cursor: pointer;
    fill: ${t.taskTextClickableColor} !important;
    font-weight: bold;
  }


  /* Specific task settings for the sections*/

  .taskText0,
  .taskText1,
  .taskText2,
  .taskText3 {
    fill: ${t.taskTextColor};
  }

  .task0,
  .task1,
  .task2,
  .task3 {
    fill: ${t.taskBkgColor};
    stroke: ${t.taskBorderColor};
  }

  .taskTextOutside0,
  .taskTextOutside2
  {
    fill: ${t.taskTextOutsideColor};
  }

  .taskTextOutside1,
  .taskTextOutside3 {
    fill: ${t.taskTextOutsideColor};
  }


  /* Active task */

  .active0,
  .active1,
  .active2,
  .active3 {
    fill: ${t.activeTaskBkgColor};
    stroke: ${t.activeTaskBorderColor};
  }

  .activeText0,
  .activeText1,
  .activeText2,
  .activeText3 {
    fill: ${t.taskTextDarkColor} !important;
  }


  /* Completed task */

  .done0,
  .done1,
  .done2,
  .done3 {
    stroke: ${t.doneTaskBorderColor};
    fill: ${t.doneTaskBkgColor};
    stroke-width: 2;
  }

  .doneText0,
  .doneText1,
  .doneText2,
  .doneText3 {
    fill: ${t.taskTextDarkColor} !important;
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
    fill: ${t.taskTextOutsideColor} !important;
  }


  /* Tasks on the critical line */

  .crit0,
  .crit1,
  .crit2,
  .crit3 {
    stroke: ${t.critBorderColor};
    fill: ${t.critBkgColor};
    stroke-width: 2;
  }

  .activeCrit0,
  .activeCrit1,
  .activeCrit2,
  .activeCrit3 {
    stroke: ${t.critBorderColor};
    fill: ${t.activeTaskBkgColor};
    stroke-width: 2;
  }

  .doneCrit0,
  .doneCrit1,
  .doneCrit2,
  .doneCrit3 {
    stroke: ${t.critBorderColor};
    fill: ${t.doneTaskBkgColor};
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
    fill: ${t.taskTextDarkColor} !important;
  }

  /* Done-crit task text outside the bar — same reasoning as doneText above. */
  .doneCritText0.taskTextOutsideLeft,
  .doneCritText0.taskTextOutsideRight,
  .doneCritText1.taskTextOutsideLeft,
  .doneCritText1.taskTextOutsideRight,
  .doneCritText2.taskTextOutsideLeft,
  .doneCritText2.taskTextOutsideRight,
  .doneCritText3.taskTextOutsideLeft,
  .doneCritText3.taskTextOutsideRight {
    fill: ${t.taskTextOutsideColor} !important;
  }

  .vert {
    stroke: ${t.vertLineColor};
  }

  .vertText {
    font-size: 15px;
    text-anchor: middle;
    fill: ${t.vertLineColor} !important;
  }

  .activeCritText0,
  .activeCritText1,
  .activeCritText2,
  .activeCritText3 {
    fill: ${t.taskTextDarkColor} !important;
  }

  .titleText {
    text-anchor: middle;
    font-size: 18px;
    fill: ${t.titleColor||t.textColor};
    font-family: ${t.fontFamily};
  }
`,"getStyles"),bi=xi,wi={parser:An,db:ki,renderer:vi,styles:bi};export{wi as diagram};
