'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const source=fs.readFileSync(require('node:path').join(__dirname,'../frontend/password-recovery.js'),'utf8');
function setup(mode,responses,hash='') {
    const elements=new Map();
    function get(id) {
        if(!elements.has(id)) elements.set(id,{value:'',textContent:'',disabled:false,dataset:{mode},handlers:{},
            addEventListener(event,fn){this.handlers[event]=fn;},reset(){get('recoveryPassword').value='';get('recoveryConfirm').value='';}});
        return elements.get(id);
    }
    const calls=[],cleaned=[];
    vm.runInNewContext(source,{document:{getElementById:get},URLSearchParams,window:{location:{hash,pathname:'/reset-password.html'},
        history:{replaceState:(...args)=>cleaned.push(args[2])}},fetch:async(url,options)=>{
            calls.push({url,options}); const result=responses.shift();
            if(result instanceof Error)throw result;
            return {ok:result.status<400,status:result.status,json:async()=>result.body};
        }});
    return {get,calls,cleaned,submit:()=>get('recoveryForm').handlers.submit({preventDefault(){}})};
}
function password(ui){ui.get('recoveryPassword').value=ui.get('recoveryConfirm').value='New-test-password';}
test('forgot request sends email only and reports queued rather than delivered',async()=>{
    const ui=setup('forgot',[{status:202,body:{success:true}}]);ui.get('recoveryEmail').value='one@example.invalid';await ui.submit();
    assert.deepEqual(JSON.parse(ui.calls[0].options.body),{email:'one@example.invalid'});
    assert.match(ui.get('recoveryMessage').textContent,/queued/);assert.doesNotMatch(ui.get('recoveryMessage').textContent,/delivered/);
});
test('reset uses fragment token privately and immediately cleans browser URL',async()=>{
    const token='x'.repeat(43);const ui=setup('reset',[{status:200,body:{success:true}}],'#token='+token);password(ui);
    assert.deepEqual(ui.cleaned,['/reset-password.html']);await ui.submit();
    assert.deepEqual(JSON.parse(ui.calls[0].options.body),{token,new_password:'New-test-password'});
    assert.equal(ui.calls[0].url,'/api/customer/reset-password');assert.equal(ui.calls[0].options.credentials,'same-origin');
    assert.doesNotMatch(ui.get('recoveryMessage').textContent,new RegExp(token));assert.equal(ui.get('recoveryPassword').value,'');
    await ui.submit();assert.equal(ui.calls.length,1);
});
test('missing duplicate or checkout-reference-only fragments cannot authorize reset',async()=>{
    for(const hash of ['', '#_ptxn=txn_untrusted','#token='+('x'.repeat(43))+'&token='+('y'.repeat(43))]){
        const ui=setup('reset',[],hash);password(ui);await ui.submit();assert.equal(ui.calls.length,0);assert.equal(ui.get('recoverySubmit').disabled,true);
    }
});
test('mismatched passwords never reach backend',async()=>{
    const ui=setup('reset',[],'#token='+'x'.repeat(43));password(ui);ui.get('recoveryConfirm').value='different';await ui.submit();assert.equal(ui.calls.length,0);
});
test('expired tokens provider configuration errors and limits show safe errors',async()=>{
    for(const status of [400,429,503]){
        const ui=setup('reset',[{status,body:{success:false,message:'PRIVATE_PROVIDER_TOKEN'}}],'#token='+'x'.repeat(43));password(ui);await ui.submit();
        assert.doesNotMatch(ui.get('recoveryMessage').textContent,/PRIVATE/);assert.equal(ui.get('recoverySubmit').disabled,false);
    }
});
test('network failure permits safe retry without exposing submitted credentials',async()=>{
    const ui=setup('reset',[new Error('PRIVATE_SECRET')],'#token='+'x'.repeat(43));password(ui);await ui.submit();
    assert.match(ui.get('recoveryMessage').textContent,/Connection failed/);assert.doesNotMatch(ui.get('recoveryMessage').textContent,/PRIVATE/);
});
