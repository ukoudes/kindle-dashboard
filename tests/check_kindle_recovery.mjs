// Optional browser regression: node tests/check_kindle_recovery.mjs
// Requires Node 22+, Chrome, and the project Python environment.
import {spawn} from 'node:child_process';
import {mkdtempSync,readFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join,dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
const root=join(dirname(fileURLToPath(import.meta.url)),'..');
const fixtureDir=mkdtempSync(join(tmpdir(),'kindle-recovery-'));
const python=process.env.KINDLE_TEST_PYTHON||join(root,'.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python');
const fixtureCode=String.raw`
import json,sys
from pathlib import Path
from dashboard.storage import Store
from dashboard.web import DashboardServer
root=Path(sys.argv[1])
(root/'hub.json').write_text(json.dumps({'local_device':{'id':'test-mac','name':'MacBook','platform':'Darwin'},'devices':[],'codexbar':{'enabled':False}}))
server=DashboardServer(('127.0.0.1',0),Store(root/'hub.sqlite3'),root/'hub.json',refresh=1)
code=server.access.create_pairing()['pairing']['code']
token=server.access.pair(code,'Recovery fixture','127.0.0.1','test')
(root/'viewer-token').write_text(token)
print('http://127.0.0.1:'+str(server.server_address[1]),flush=True)
server.serve_forever()
`;
const fixture=spawn(python,['-c',fixtureCode,fixtureDir],{cwd:root,stdio:['ignore','pipe','inherit']});
const baseURL=await new Promise((resolve,reject)=>{
 const timer=setTimeout(()=>reject(Error('Fixture startup timed out')),10000);
 let output='';
 fixture.on('error',reject);fixture.on('exit',()=>reject(Error('Fixture exited')));
 fixture.stdout.on('data',chunk=>{output+=chunk;if(output.includes('\n')){clearTimeout(timer);resolve(output.trim())}});
});
const profile=mkdtempSync(join(tmpdir(),'kindle-layout-'));
const proc=spawn(process.env.CHROME_BINARY||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',['--headless','--disable-gpu','--no-first-run','--no-default-browser-check','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],{stdio:'ignore'});
let ws;
try{
 let port;
 for(let i=0;i<70;i++){try{port=Number(readFileSync(join(profile,'DevToolsActivePort'),'utf8').split('\n')[0]);break}catch{}await new Promise(r=>setTimeout(r,100))}
 if(!port)throw Error('Browser startup timed out');
 const tabs=await(await fetch('http://127.0.0.1:'+port+'/json')).json();
 ws=new WebSocket(tabs[0].webSocketDebuggerUrl);
 await new Promise((res,rej)=>{ws.onopen=res;ws.onerror=rej});
 let seq=0;const pending=new Map();
 ws.onmessage=({data})=>{const r=JSON.parse(data);const p=pending.get(r.id);if(p){pending.delete(r.id);r.error?p.reject(Error(r.error.message)):p.resolve(r.result)}};
 const send=(method,params={})=>new Promise((resolve,reject)=>{const id=++seq;pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}))});
 await send('Page.enable');
 await send('Network.enable');
 await send('Network.setCookie',{name:'kindle_access',value:readFileSync(join(fixtureDir,'viewer-token'),'utf8'),url:baseURL,httpOnly:true,sameSite:'Strict'});
 await send('Emulation.setDeviceMetricsOverride',{width:600,height:800,deviceScaleFactor:1,mobile:true});
 const evaluate=async expression=>(await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true})).result.value;
 const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
 await send('Page.navigate',{url:baseURL+'/kindle'});
 await pause(600);
 if(!await evaluate("document.querySelector('img')?.naturalWidth===1072"))throw Error('Initial image missing');
 await send('Network.emulateNetworkConditions',{offline:true,latency:0,downloadThroughput:0,uploadThroughput:0});
 await pause(2400);
 if(!await evaluate("document.querySelector('img')?.naturalWidth===1072"))throw Error('Offline lost last good image');
 if(!await evaluate("document.getElementById('connection')?.textContent.includes('自动重试') && !document.getElementById('connection').hidden"))throw Error('Offline state missing');
 console.log('Offline retains readable picture and reports retry');
 await send('Network.emulateNetworkConditions',{offline:false,latency:0,downloadThroughput:-1,uploadThroughput:-1});
 await pause(2600);
 if(!await evaluate("document.querySelector('img')?.naturalWidth===1072 && document.getElementById('connection')?.hidden"))throw Error('Did not recover without page reload');
 console.log('Network restoration resumes automatically');
 await send('Runtime.evaluate',{expression:"document.querySelector('[aria-label=API]').click()"});
 await pause(600);
 if(!await evaluate("location.search.includes('view=api') && document.querySelector('img')?.naturalWidth===1072"))throw Error('Tab navigation failed');
 console.log('API tab navigation passed');
}catch(e){console.error(e.message);process.exitCode=1}finally{
 ws?.close();proc.kill('SIGTERM');fixture.kill('SIGTERM');
 await new Promise(resolve=>proc.exitCode!==null?resolve():proc.once('exit',resolve));
 await new Promise(resolve=>fixture.exitCode!==null||fixture.signalCode!==null?resolve():fixture.once('exit',resolve));
 rmSync(profile,{recursive:true,force:true});rmSync(fixtureDir,{recursive:true,force:true});
}
