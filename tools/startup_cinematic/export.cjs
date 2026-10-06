const {chromium} = require(process.env.NEXUS_PLAYWRIGHT_MODULE || 'playwright');
const fs = require('fs');
const path = require('path');
const {spawn} = require('child_process');
const {once} = require('events');
const {serve,root} = require('./server.cjs');
const work = path.join(__dirname,'work'); fs.mkdirSync(work,{recursive:true});
const review = path.join(root,'docs/review/startup-milestones'); fs.mkdirSync(review,{recursive:true});
const timeline = require('../../desktop/ChatNexus.Desktop/splash/startup_caption_timeline.json');
function encoder(name) {
  const child = spawn('ffmpeg',['-y','-hide_banner','-loglevel','error','-f','image2pipe','-framerate','30','-vcodec','png','-i','pipe:0','-an','-vf','scale=1280:720:flags=lanczos','-c:v','libx264','-preset','fast','-crf','18','-pix_fmt','yuv420p','-g','42','-keyint_min','42','-sc_threshold','0','-force_key_frames','12.2,24.4','-movflags','+faststart',path.join(work,name+'.mp4')],{windowsHide:true});
  const done = once(child,'close'); child.stderr.on('data',d=>process.stderr.write(d));
  return {child,done};
}
(async()=>{
  const {server,url} = await serve();
  const browser = await chromium.launch({...(process.platform==='win32'?{channel:'msedge'}:{}),headless:true});
  const errors = [];
  try {
    const page = await browser.newPage({viewport:{width:1024,height:576},deviceScaleFactor:1});
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto(url+'/tools/startup_cinematic/author.html');
    await page.waitForFunction(()=>Boolean(window.author));
    const optionalFits = await page.evaluate(entries => entries.map(entry => {
      const box=document.querySelector('#current'),p=box.querySelector('.primary'),s=box.querySelector('.secondary');
      p.textContent=entry.primary;s.textContent=entry.secondary;
      return {primary:entry.primary,fits:p.scrollWidth<=box.clientWidth&&s.scrollWidth<=box.clientWidth};
    }),timeline.optionalCaptions);
    if(optionalFits.some(e=>!e.fits))throw Error('Optional caption overflows');
    const states = [];
    const alignment=[];
    for(const t of [0,1,3.5,5.4,8.9,11.6,14,24.4,29.8]) {
      await page.evaluate(t=>window.author.render(t,true),t);
      const clean=await page.screenshot({clip:{x:0,y:0,width:1024,height:472}});
      await page.evaluate(()=>window.author.clean(false));
      const reference=await page.screenshot({clip:{x:0,y:0,width:1024,height:472}});
      if(!clean.equals(reference))throw Error('Unencoded scene mismatch at '+t);
      alignment.push({at:t,scenePixelsIdentical:true});
    }
    fs.writeFileSync(path.join(review,'source-frame-alignment.json'),JSON.stringify(alignment,null,2)+'\n');
    for (const item of timeline.captions) {
      const t = item.id==='finalizing' ? timeline.finalizingHold.at : item.at + .4;
      const state = await page.evaluate(t=>window.author.render(t),t);
      if (state.primary!==item.primary || state.secondary!==item.secondary || !state.fits) throw Error('Caption mismatch/overflow: '+item.id);
      states.push(state);
      await page.screenshot({path:path.join(work,item.id+'.png')});
    }
    const loopPNGs = [];
    for (const [id,t] of [['online-low',24.8],['online-peak',25.5],['loop-start',24.4],['loop-end',30]]) {
      const state = await page.evaluate(t=>window.author.render(t),t);
      const png = await page.screenshot({path:path.join(work,id+'.png')});
      if(id.startsWith('loop'))loopPNGs.push(png);
      states.push(state);
    }
    if (!loopPNGs[0].equals(loopPNGs[1])) throw Error('Loop endpoints are not pixel identical');
    const allFrames = [];
    let clean, reference;
    if(!process.argv.includes('--stills')) {
      if(!process.argv.includes('--reference-only'))clean=encoder('clean-silent');
      reference=encoder('reference-silent');
    }
    try {
      for(let i=0;i<timeline.duration*timeline.fps;i++) {
        const state = await page.evaluate(t=>window.author.render(t,true),i/timeline.fps);
        if(!state.fits || (state.progress>=1 && i<Math.round(timeline.onlineTail.start*timeline.fps))) throw Error('Invalid frame '+i);
        allFrames.push(state);
        if(clean) {
          const png = await page.screenshot({type:'png'});
          if(!clean.child.stdin.write(png))await once(clean.child.stdin,'drain');
        }
        if(reference) {
          await page.evaluate(()=>window.author.clean(false));
          const labeled = await page.screenshot({type:'png'});
          if(!reference.child.stdin.write(labeled))await once(reference.child.stdin,'drain');
        }
        if(i%90===0)console.log(i+'/900: '+state.primary);
      }
      const encoders=[clean,reference].filter(Boolean);
      for(const e of encoders)e.child.stdin.end();
      for(const code of await Promise.all(encoders.map(e=>e.done)))if(code[0]!==0)throw Error('Encoder failed '+code);
    } finally {for(const e of [clean,reference].filter(Boolean))e.child.kill();}
    if(errors.length)throw Error(errors.join('\n'));
    fs.writeFileSync(path.join(review,'authoring-verification.json'),JSON.stringify({fps:30,frames:allFrames.length,loopEndpointPixelsIdentical:true,captionBounds:timeline.captions.map(c=>({id:c.id,at:c.at,frame:allFrames.findIndex(f=>f.id===c.id)})),hold:states.find(s=>s.id==='finalizing'),pulse:states.slice(-4,-2),captionOverflowFrames:allFrames.filter(s=>!s.fits).length,preOnlineFullProgressFrames:allFrames.filter(s=>s.t<12.2&&s.progress>=1).length,errors},null,2)+'\n');
    fs.writeFileSync(path.join(review,'optional-caption-fit.json'),JSON.stringify(optionalFits,null,2)+'\n');
    fs.writeFileSync(path.join(work,'frames.json'),JSON.stringify(allFrames));
    console.log('Authoring checks passed; 900 aligned frames per master.');
  } finally {await browser.close();server.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
