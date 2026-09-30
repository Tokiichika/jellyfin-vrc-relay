/* Live management shares the existing session, API, settings and toast lifecycle. */
(() => {
  let timer, active=false, loading=false, rows=new Map(), snapshot=null;
  const view=document.createElement('div'); view.id='live-view'; view.hidden=true;
  view.innerHTML=`<div class="live-hero"><div><span class="eyebrow">RIGHT HERE, RIGHT NOW</span><h1>直播管理</h1><p>将此刻分享给同一个房间。</p></div><span class="live-label" id="live-engine">正在连接引擎…</span></div>
    <p id="live-error" role="status"></p><div class="live-metrics" id="live-metrics"></div>
    <section class="live-toolbar"><div><h2>观看控制</h2><p id="live-capacity"></p><small>直播与点播独立限速；两者预算相加应低于服务器出口带宽。每出口 IP 准入保底 10 Mbps，同 IP 的连接共享额度。</small></div><div class="live-actions"><a href="#settings">调整带宽设置 ↗</a><button id="live-refresh" class="secondary">刷新直播状态</button><button id="live-pause-all" class="danger">清退并暂停全部直播观看</button></div></section>
    <div class="live-layout"><section class="live-create"><span class="eyebrow">GO LIVE</span><h2>创建直播间</h2><form id="live-create-form"><label for="live-title">直播间名称</label><input id="live-title" required maxlength="120" placeholder="今晚一起看 / 游戏直播"><button id="live-create-button">创建直播间</button></form><details><summary>OBS 与 VRChat 怎么设置？</summary><ol><li>OBS → 设置 → 直播 → 自定义，填入直播间的推流服务器和串流密钥。</li><li>输出 H.264 视频与 AAC 音频，建议 1080p / 30 fps、CBR 6 Mbps、AAC 192 kbps、关键帧间隔 2 秒。</li><li>开始推流，等待直播间显示正在直播，再将播放地址粘贴到支持 RTSP TCP 的 VRChat PC 播放器。</li></ol><p>只做实时转发，无录制、回看、预载或拖动。实际延迟取决于编码和世界播放器的缓冲。</p><p>推流及播放走独立 TCP 端口，首次部署需放行 1935 / 8554。域名应直连服务器；HTTP CDN 和网站反向代理不负责转发它们。</p></details></section><div id="live-rooms" aria-live="polite"></div></div>
    <footer>在线 IP / 连接数来自直播网关，不等于 VRChat 房间人数。发送速率是网关响应字节，推流速率来自直播引擎，约 3 秒采样。正在直播表示引擎已收到媒体轨道，不保证 OBS 编码正确。RTMP 与 RTSP TCP 未加密，请妥善保管地址及推流密钥。</footer>`;
  $('management').append(view);
  for(const [id,label] of [['input','OBS 推流输入'],['output','向直播观众发送'],['ips','观看出口 IP'],['rooms','正在直播']]){
    const box=document.createElement('div');box.className='metric';
    const name=document.createElement('small'),value=document.createElement('strong');
    name.textContent=label;value.id='live-metric-'+id;value.textContent='—';box.append(name,value);$('live-metrics').append(box);
  }
  async function copy(value){try{await navigator.clipboard.writeText(value);notify('复制成功')}catch{throw Error('复制失败，请展开连接信息后手动复制')}}
  async function control(operation,id){const result=await api('/api/live','POST',{operation,id});await refresh();if(result.warning)throw Error(result.warning);notify('直播操作成功')}
  function makeRow(room){
    const node=document.createElement('article');node.className='live-room';
    node.innerHTML='<div class="section-heading"><h2></h2><span class="live-label"></span></div><p class="live-room-detail"></p><div class="live-actions"></div><details><summary>OBS 连接信息与推流密钥</summary><div class="live-credentials"></div><small>播放地址是观看凭证；推流密钥仅供 OBS 使用，请勿分享到房间。重置推流密钥不会更改播放地址。</small><div class="live-admin-actions"></div></details>';
    const model={node,room};
    const button=(label,operation,container,confirmText)=>{const b=document.createElement('button');b.type='button';b.textContent=label;b.className='secondary';b.onclick=()=>action(()=>withBusy(b,'处理中…',async()=>{if(confirmText&&!confirm(confirmText))return;await control(typeof operation==='function'?operation(model.room):operation,model.room.id)}));container.append(b);return b};
    const actions=node.querySelector('.live-actions');
    const play=document.createElement('button');play.textContent='复制播放地址';play.onclick=()=>action(()=>copy(model.room.play_url));actions.append(play);
    model.pause=button('清退并暂停观看',r=>r.paused?'resume':'pause',actions,'切换此直播间的观看状态？暂停时会断开当前观众，并阻止自动重连。');
    const credentials=node.querySelector('.live-credentials');
    for(const [key,label,secret] of [['obs_server','OBS 服务器',false],['obs_key','OBS 串流密钥',true],['play_url','VRChat 播放地址',false]]){
      const wrapper=document.createElement('label');wrapper.textContent=label;const row=document.createElement('div');row.className='row';const input=document.createElement('input');input.readOnly=true;input.type=secret?'password':'text';input.setAttribute('aria-label',label);model[key]=input;
      const b=document.createElement('button');b.type='button';b.textContent='复制';b.className='secondary';b.onclick=()=>action(()=>copy(model.room[key]));row.append(input,b);
      if(secret){const reveal=document.createElement('button');reveal.type='button';reveal.className='secondary';reveal.textContent='显示';reveal.onclick=()=>{input.type=input.type==='password'?'text':'password';reveal.textContent=input.type==='password'?'显示':'隐藏'};row.append(reveal)}
      wrapper.append(row);credentials.append(wrapper);
    }
    const admin=node.querySelector('.live-admin-actions');
    model.enable=button('停用直播间',r=>r.enabled?'disable':'enable',admin,'切换直播间启用状态？停用会断开推流和观众，启用后 OBS 需重新推流。');
    button('重置推流密钥','rotate',admin,'旧推流密钥将立即失效，并断开当前 OBS 推流；确认重置？');
    button('删除直播间','delete',admin,'删除后推流与播放地址永久失效，当前观众将被断开。确认删除？');
    return model;
  }
  function render(data){
    snapshot=data;
    $('live-engine').textContent=!data.enabled?'直播未启用':data.online&&data.gateway?'● 引擎在线':'○ 引擎未连接';
    $('live-error').textContent=!data.enabled?'请使用新版 install.sh 或更新脚本启用直播容器。':data.error;
    $('live-metric-input').textContent=data.input_mbps.toFixed(2)+' Mbps';$('live-metric-output').textContent=data.output_mbps.toFixed(2)+' Mbps';
    $('live-metric-ips').textContent=data.bandwidth.admitted_ips+' / '+data.bandwidth.capacity;
    $('live-metric-rooms').textContent=data.rooms.filter(r=>r.ready).length;
    $('live-capacity').textContent=`${data.paused?'全部观看已暂停':'允许观看'} · 总上限 ${data.bandwidth.effective_total_mbps} Mbps · 当前单 IP 上限 ${data.bandwidth.effective_client_mbps.toFixed(1)} Mbps`;
    $('live-pause-all').textContent=data.paused?'恢复全部直播观看':'清退并暂停全部直播观看';
    const ids=new Set(data.rooms.map(r=>r.id));for(const [id,row] of rows)if(!ids.has(id)){row.node.remove();rows.delete(id)}
    $('live-rooms').querySelector('.empty')?.remove();
    for(const room of data.rooms){let row=rows.get(room.id);if(!row){row=makeRow(room);rows.set(room.id,row);$('live-rooms').append(row.node)}row.room=room;row.node.querySelector('h2').textContent=room.title;
      row.node.querySelector('.live-label').textContent=!room.enabled?'已停用':room.ready?'● 正在直播':'等待 OBS 推流';row.node.classList.toggle('on-air',room.ready);
      row.node.querySelector('.live-room-detail').textContent=`${room.clients} 个观看 IP · ${room.connections} 条连接 · ${(room.tracks||[]).join(' / ')||'尚无媒体轨道'}${room.paused?' · 本直播间观看已暂停':''}`;
      row.pause.textContent=room.paused?'恢复观看':'清退并暂停观看';row.enable.textContent=room.enabled?'停用直播间':'启用直播间';
      for(const key of ['obs_server','obs_key','play_url'])if(row[key].value!==room[key])row[key].value=room[key];
    }
    if(!rows.size){const empty=document.createElement('div');empty.className='empty';empty.textContent='还没有直播间。在左侧创建一个，开始分享此刻。';$('live-rooms').append(empty)}
  }
  async function refresh(){if(loading||!connected)return;loading=true;try{const data=await api('/api/live');if(connected&&active)render(data)}finally{loading=false}}
  function poll(){clearTimeout(timer);if(!active||!connected)return;refresh().catch(e=>{if(active&&connected)$('live-error').textContent=e.message}).finally(()=>{if(active&&connected)timer=setTimeout(poll,refreshSeconds*1000)})}
  $('live-create-form').onsubmit=e=>{e.preventDefault();action(()=>withBusy($('live-create-button'),'创建中…',async()=>{const result=await api('/api/live','POST',{operation:'create',title:$('live-title').value});notify('直播间创建成功');await refresh()}))};
  $('live-refresh').onclick=()=>action(()=>withBusy($('live-refresh'),'刷新中…',async()=>{await refresh();notify('直播状态已刷新')}));
  $('live-pause-all').onclick=()=>action(()=>withBusy($('live-pause-all'),'处理中…',async()=>{if(!snapshot)return;if(!snapshot.paused&&!confirm('清退所有直播观众并暂停新观看？OBS 推流继续，点播不受影响。'))return;await control(snapshot.paused?'resume_all':'pause_all')}));
  window.unifiedLive={enter(open){active=open;view.hidden=!open;clearTimeout(timer);if(open)poll()},reset(){active=false;clearTimeout(timer);rows.clear();snapshot=null;$('live-rooms').replaceChildren();$('live-title').value='';view.hidden=true}};
  window.unifiedSettings?.route();
})();
