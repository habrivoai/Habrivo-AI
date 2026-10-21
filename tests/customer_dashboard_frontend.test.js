'use strict';
const {test}=require('node:test'), assert=require('node:assert/strict'), vm=require('node:vm'), fs=require('node:fs');
const source=fs.readFileSync(require('node:path').join(__dirname,'../frontend/customer-dashboard.js'),'utf8');
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function response(body,status=200){return {ok:status<400,status,json:async()=>body};}
const user={id:1,name:'<script>Synthetic</script>',email:'one@example.invalid'};
function setup(responder,search=''){
    const nodes=new Map(),events=new Map(),windows=new Map(),calls=[];
    function get(id){
        if(!nodes.has(id)){
            const node={value:'',textContent:'',disabled:false,required:false,hidden:false,children:[],handlers:{},clicks:0,scrolls:0,
                addEventListener(name,fn){this.handlers[name]=fn;},replaceChildren(){this.children=[];},
                click(){this.clicks++;this.handlers.click?.();},scrollIntoView(){this.scrolls++;}};
            node.classList={toggle(name,value){node.hidden=value;},add(){node.hidden=true;},remove(){node.hidden=false;}};
            nodes.set(id,node);
        }return nodes.get(id);
    }
    const links=['library','account','logout'].map(action=>{const node=get(action+'Link');node.dataset={dashboardAction:action};return node;});
    vm.runInNewContext(source,{document:{getElementById:get,querySelectorAll:()=>links,
        addEventListener:(name,fn)=>events.set(name,fn),dispatchEvent:event=>events.get(event.type)?.(event)},
        window:{location:{search},addEventListener:(name,fn)=>windows.set(name,fn)},URLSearchParams,
        Event:class{constructor(type){this.type=type;}},fetch:async(url,options)=>{calls.push({url,options});return responder(url,options);}});
    return {get,calls,links,ended:()=>events.get('customer-session-ended')(),
        hide:()=>windows.get('pagehide')(),back:()=>windows.get('pageshow')({persisted:true}),
        submit:()=>get('authForm').handlers.submit({preventDefault(){}})};
}
test('existing session opens dashboard and renders server name as plain text',async()=>{
    const ui=setup(()=>response({authenticated:true,user}));await flush();
    assert.equal(ui.get('welcome').hidden,false);assert.equal(ui.get('authForm').hidden,true);
    assert.equal(ui.get('welcomeName').textContent,'Welcome, <script>Synthetic</script>!');
    assert.equal(ui.calls[0].options.cache,'no-store');
});
test('successful login opens dashboard and clears login password',async()=>{
    const results=[response({},401),response({success:true,user})];const ui=setup(()=>results.shift());await flush();
    ui.get('email').value='one@example.invalid';ui.get('password').value='Synthetic-password';await ui.submit();
    assert.equal(ui.get('welcome').hidden,false);assert.equal(ui.get('password').value,'');
    assert.equal(ui.calls[1].url,'/api/customer/login');assert.equal(ui.calls[1].options.credentials,'same-origin');
});
test('signup remains separate from login and never fabricates a paid account',async()=>{
    const results=[response({},401),response({success:true},201)];const ui=setup(()=>results.shift(),'?mode=signup&_ptxn=untrusted');await flush();
    ui.get('email').value='one@example.invalid';ui.get('name').value='Synthetic';ui.get('password').value='Synthetic-password';await ui.submit();
    assert.equal(ui.calls[1].url,'/api/register');assert.equal(ui.get('welcome').hidden,true);
    assert.match(ui.get('message').textContent,/Sign in/);assert.equal(ui.get('submitBtn').textContent,'Login');
});
test('session check errors show retry without presenting stale private data',async()=>{
    const ui=setup(()=>response({authenticated:false},503));await flush();
    assert.equal(ui.get('welcome').hidden,true);assert.equal(ui.get('sessionRetry').hidden,false);
    assert.match(ui.get('message').textContent,/Retry/);
});
test('expired session clears private prompts, results, library and account profile',async()=>{
    const ui=setup(()=>response({authenticated:true,user}));await flush();
    ui.get('marketing_promotional_copy').value='PRIVATE_COPY';ui.get('accountProfile').textContent='PRIVATE_EMAIL';
    ui.get('libraryItems').children.push('PRIVATE_PACK');ui.ended();
    assert.equal(ui.get('welcome').hidden,true);assert.equal(ui.get('marketing_promotional_copy').value,'');
    assert.equal(ui.get('accountProfile').textContent,'');assert.equal(ui.get('libraryItems').children.length,0);
    assert.equal(ui.get('marketingFields').disabled,true);
});
test('late login response cannot reopen a dashboard after session termination',async()=>{
    let finish;const ui=setup(()=>new Promise(resolve=>{finish=resolve;}));
    ui.ended();finish(response({authenticated:true,user}));await flush();assert.equal(ui.get('welcome').hidden,true);
});
test('workspace links reuse the library and settings controls',async()=>{
    const ui=setup(()=>response({authenticated:true,user}));await flush();
    ui.links[0].handlers.click({preventDefault(){}});ui.links[1].handlers.click({preventDefault(){}});
    assert.equal(ui.get('productsBtn').clicks,1);assert.equal(ui.get('accountBtn').clicks,1);
    ui.links[2].handlers.click({preventDefault(){}});assert.equal(ui.get('logoutBtn').clicks,1);
    ui.get('aiChatBtn').handlers.click();assert.equal(ui.get('marketingStudio').scrolls,1);
});
test('back-forward cached pages clear data and revalidate before showing workspace',async()=>{
    let active=true;const ui=setup(()=>active?response({authenticated:true,user}):response({},401));await flush();
    ui.hide();assert.equal(ui.get('welcome').hidden,true);active=false;ui.back();await flush();
    assert.equal(ui.get('welcome').hidden,true);assert.equal(ui.get('authForm').hidden,false);
});
test('wrong password and limits never reveal a dashboard or raw server errors',async()=>{
    const results=[response({},401),response({success:false,message:'PRIVATE_ERROR'},401),response({},429)];
    const ui=setup(()=>results.shift());await flush();await ui.submit();assert.match(ui.get('message').textContent,/Invalid email or password/);
    await ui.submit();assert.match(ui.get('message').textContent,/Too many/);assert.equal(ui.get('welcome').hidden,true);
});
