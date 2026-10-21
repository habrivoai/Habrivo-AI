'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const script = fs.readFileSync(require('node:path').join(__dirname,'../frontend/email-verification.js'),'utf8');
const token = 'a'.repeat(43);
function setup({verify=true,hash='#token='+token,respond=async()=>({ok:true,status:200,json:async()=>({success:true})})}={}) {
    const nodes=new Map(),events=new Map(),calls=[],history=[];
    const get=id=>{
        if ((id==='verificationForm'&&!verify)||(id==='verificationResend'&&verify)) return null;
        if(!nodes.has(id)) nodes.set(id,{disabled:false,textContent:'',handlers:{},addEventListener(name,fn){this.handlers[name]=fn;}});
        return nodes.get(id);
    };
    vm.runInNewContext(script,{
        document:{getElementById:get,addEventListener:(name,fn)=>events.set(name,fn)},
        window:{location:{hash,pathname:'/verify-email.html'},history:{replaceState:(...args)=>history.push(args)}},URLSearchParams,
        fetch:async(url,options)=>{calls.push({url,options});return respond();}
    });
    return {get,calls,history,ended:()=>events.get('customer-session-ended')(),
        submit:()=>get(verify?'verificationForm':'verificationResend').handlers[verify?'submit':'click']({preventDefault(){}})};
}
test('verification bearer is removed from URL and submitted once, never used for login',async()=>{
    const ui=setup();assert.equal(ui.history[0][2],'/verify-email.html');await ui.submit();await ui.submit();
    assert.equal(ui.calls.length,1);assert.equal(ui.calls[0].url,'/api/customer/verify-email');
    assert.deepEqual(JSON.parse(ui.calls[0].options.body),{token});
    assert.equal(ui.get('verificationSubmit').disabled,true);
    assert.match(ui.get('verificationMessage').textContent,/Email verified/);
    assert.doesNotMatch(ui.get('verificationMessage').textContent,new RegExp(token));
});
test('missing, duplicate or checkout-only tokens cannot trigger verification',async()=>{
    for(const hash of ['','#token=short','#token='+token+'&token='+token,'#_ptxn='+token]){
        const ui=setup({hash});await ui.submit();assert.equal(ui.calls.length,0);assert.equal(ui.get('verificationSubmit').disabled,true);
    }
});
test('expired link, rate limit and private error payload use safe messages',async()=>{
    for(const status of [400,429,503]){
        const ui=setup({respond:async()=>({ok:false,status,json:async()=>({success:false,message:'PRIVATE_TOKEN'})})});
        await ui.submit();assert.doesNotMatch(ui.get('verificationMessage').textContent,/PRIVATE_TOKEN/);
        assert.match(ui.get('verificationMessage').textContent,status===429?/Too many/:/expired, used or invalid/);
    }
});
test('resend sends no identity selector and truthfully reports queued work',async()=>{
    const ui=setup({verify:false});await ui.submit();
    assert.equal(ui.calls[0].url,'/api/customer/resend-verification');assert.equal(ui.calls[0].options.body,'{}');
    assert.match(ui.get('verificationMessage').textContent,/queued, not yet delivered/);
});
test('unconfigured SMTP and unreadable responses never claim delivery',async()=>{
    for(const respond of [async()=>({ok:false,status:503,json:async()=>({success:false,message:'PRIVATE'})}),
        async()=>({ok:false,status:503,json:async()=>{throw new Error('PRIVATE');}})]){
        const ui=setup({verify:false,respond});await ui.submit();
        assert.doesNotMatch(ui.get('verificationMessage').textContent,/PRIVATE|queued|delivered/);
        assert.equal(ui.get('verificationResend').disabled,false);
    }
});
test('pending requests prevent duplicate sends and ignore responses after logout',async()=>{
    let finish;const ui=setup({verify:false,respond:()=>new Promise(resolve=>{finish=resolve;})});
    const pending=ui.submit();await ui.submit();assert.equal(ui.calls.length,1);ui.ended();
    finish({ok:true,status:202,json:async()=>({success:true})});await pending;
    assert.equal(ui.get('verificationMessage').textContent,'');assert.equal(ui.get('verificationResend').disabled,true);
});
