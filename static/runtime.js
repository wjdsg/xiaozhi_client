(function(){
'use strict';

const escapeHtml=function(value){
  return String(value==null?'':value).replace(/[&<>'"]/g,function(ch){
    return {'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch];
  });
};

const timeText=function(){
  const now=new Date();
  return String(now.getHours()).padStart(2,'0')+':'+String(now.getMinutes()).padStart(2,'0');
};

const dateText=function(){
  const now=new Date();
  return (now.getMonth()+1)+'月'+now.getDate()+'日 星期'+['日','一','二','三','四','五','六'][now.getDay()];
};

const statusIcon=function(state){
  const klass=state==='idle'||state==='listening'||state==='thinking'||state==='speaking'?'online':state==='connecting'?'connecting':'offline';
  return '<i class="ti ti-wifi runtime-status '+klass+'" aria-hidden="true"></i>';
};

const dialogHeader=function(){
  let right='<i class="ti ti-microphone-off micoff" aria-hidden="true"></i>';
  if(Runtime.state==='listening')right='<i class="ti ti-microphone micdot" aria-hidden="true"></i>';
  if(Runtime.state==='speaking')right='<div class="wvb" style="height:10px;width:4px"></div><div class="wvb" style="height:15px;width:4px;animation-delay:.2s"></div><div class="wvb" style="height:11px;width:4px;animation-delay:.4s"></div>';
  return '<div class="topbar"><span>'+timeText()+'</span><span>'+statusIcon(Runtime.state)+'</span></div>'+
    '<div class="head">'+mascot(30,true)+'<span class="head-name">灵犀</span><div class="head-right">'+right+'</div></div>';
};

const LiveDialog=(function(){
  let messages=[];
  let lastUserText='';
  let typeTimer=null;

  function clearTypeTimer(){
    if(typeTimer){clearInterval(typeTimer);typeTimer=null;}
  }

  function chatHtml(){
    return messages.map(function(item){
      return '<div class="'+(item.role==='user'?'qb':'ab')+'">'+escapeHtml(item.text)+'</div>';
    }).join('');
  }

  function centerHtml(){
    if(Runtime.state==='listening'){
      const waves=[12,20,16,22,13].map(function(h,i){return '<div class="wvb" style="height:'+h+'px;animation-delay:'+(i*.15)+'s"></div>';}).join('');
      return '<div class="runtime-center fade">'+mascot(112)+'<div class="runtime-title">我在</div><div style="display:flex;gap:5px;height:22px;align-items:center">'+waves+'</div><div class="runtime-sub">正在聆听，请直接说话</div></div><div class="glowbar"></div>';
    }
    if(Runtime.state==='connecting')return '<div class="runtime-center fade">'+mascot(100)+'<div class="runtime-title">正在连接</div><div class="runtime-sub">正在准备语音服务…</div></div>';
    if(Runtime.state==='disconnected')return '<div class="runtime-center fade">'+mascot(100,true)+'<div class="runtime-title">连接已断开</div><div class="runtime-sub">点击下方“重新连接”继续使用</div></div>';
    return '<div class="runtime-center fade"><div style="font-size:58px;font-weight:500;color:#24507e;letter-spacing:4px">'+timeText()+'</div><div style="font-size:15px;color:#5b82ab">今天也很棒，一起加油！</div><div style="display:flex;align-items:center;gap:7px;margin-top:8px"><span class="breath-dot" style="width:10px;height:10px;border-radius:50%;background:#2b9bf4;animation:brea 2.6s infinite ease-in-out"></span><span class="runtime-sub">我在</span></div></div>';
  }


  function dialogActionsHtml(){
    const primaryLabels={
      connecting:'<i class="ti ti-loader" aria-hidden="true"></i> 连接中',
      disconnected:'<i class="ti ti-plug-connected" aria-hidden="true"></i> 重新连接',
      idle:'<i class="ti ti-microphone" aria-hidden="true"></i> 开始对话',
      listening:'<i class="ti ti-player-stop" aria-hidden="true"></i> 停止',
      thinking:'<i class="ti ti-player-stop" aria-hidden="true"></i> 停止',
      speaking:'<i class="ti ti-player-stop" aria-hidden="true"></i> 打断'
    };
    return '<div class="runtime-dialog-actions">'+
      '<button class="runtime-dialog-primary" onclick="Runtime.primaryAction()" '+(Runtime.state==='connecting'?'disabled':'')+'>'+primaryLabels[Runtime.state]+'</button>'+
      '<button onclick="Runtime.reconnect()"><i class="ti ti-refresh" aria-hidden="true"></i> 重连</button>'+
      '<button class="'+(Runtime.wakeWordEnabled?'on':'')+'" onclick="Runtime.toggleWakeWord()"><i class="ti ti-bell-ringing" aria-hidden="true"></i> 唤醒词'+(Runtime.wakeWordEnabled?'开':'关')+'</button>'+
      '</div>';
  }
  function render(){
    if(currentApp!=='dialog')return;
    let body;
    if((Runtime.state==='listening'&&!messages.length)||(!messages.length&&Runtime.state!=='thinking'&&Runtime.state!=='speaking')){
      body=centerHtml();
    }else{
      body='<div id="runtimeChat" class="runtime-chat fade">'+chatHtml()+(Runtime.state==='thinking'?'<div class="runtime-thinking"><i></i><i></i><i></i></div>':'')+'</div>'+(Runtime.state==='speaking'?'<div class="glowbar"></div>':'');
    }
    $('lamp').innerHTML='<div class="screen-page runtime-dialog-page">'+dialogHeader()+body+dialogActionsHtml()+'</div>';
    const chat=$('runtimeChat');
    if(chat)chat.scrollTop=chat.scrollHeight;
    updateControls();
  }

  function updateControls(){
    if(currentApp!=='dialog')return;
    const btn=$('btnPlay');
    const labels={
      connecting:'<i class="ti ti-loader" aria-hidden="true"></i> 连接中',
      disconnected:'<i class="ti ti-plug-connected" aria-hidden="true"></i> 重新连接',
      idle:'<i class="ti ti-microphone" aria-hidden="true"></i> 开始对话',
      listening:'<i class="ti ti-player-stop" aria-hidden="true"></i> 停止',
      thinking:'<i class="ti ti-player-stop" aria-hidden="true"></i> 停止',
      speaking:'<i class="ti ti-player-stop" aria-hidden="true"></i> 打断'
    };
    btn.innerHTML=labels[Runtime.state]||labels.idle;
    btn.disabled=Runtime.state==='connecting';
    $('btnRe').innerHTML='<i class="ti ti-refresh" aria-hidden="true"></i> 重连';
    $('btnSnd').innerHTML='<i class="ti ti-bell-ringing" aria-hidden="true"></i> 唤醒词'+(Runtime.wakeWordEnabled?'开':'关');
    $('voiceSel').classList.add('hidden');
    $('controls').classList.add('hidden');
    $('cap').textContent=Runtime.errorMessage||({connecting:'正在连接语音服务',disconnected:'服务已断开，可点击重新连接',idle:Runtime.wakeWordEnabled?'说“你好灵犀”也可以直接唤醒':'点击开始对话',listening:'正在聆听…',thinking:'正在思考…',speaking:'正在播报，点击可打断'}[Runtime.state]||'');
  }

  function updateUser(text){
    if(!text)return;
    lastUserText=text;
    const last=messages[messages.length-1];
    if(last&&last.role==='user')last.text=text;
    else messages.push({role:'user',text:text});
    render();
  }

  function typeAnswer(text){
    clearTypeTimer();
    let answer=messages[messages.length-1];
    if(!answer||answer.role!=='assistant'){
      answer={role:'assistant',text:''};
      messages.push(answer);
    }
    let index=Math.min(answer.text.length,text.length);
    answer.text=text.slice(0,index);
    render();
    typeTimer=setInterval(function(){
      index++;
      answer.text=text.slice(0,index);
      const bubbles=document.querySelectorAll('#runtimeChat .ab');
      const el=bubbles[bubbles.length-1];
      if(el)el.textContent=answer.text;
      const chat=$('runtimeChat');
      if(chat)chat.scrollTop=chat.scrollHeight;
      if(index>=text.length)clearTypeTimer();
    },30);
  }

  return{
    start:function(){
      $('controls').classList.add('hidden');
      $('voiceSel').classList.add('hidden');
      render();
    },
    stop:clearTypeTimer,
    refresh:render,
    replay:function(){Runtime.reconnect();},
    togglePlay:function(){Runtime.primaryAction();return Runtime.state!=='idle';},
    render:render,
    updateControls:updateControls,
    clear:function(){clearTypeTimer();messages=[];lastUserText='';render();},
    onSTT:updateUser,
    onTTS:function(data){
      if(data.state==='start' && lastUserText){
        const last=messages[messages.length-1];
        if(!last || last.role!=='user')messages.push({role:'user',text:lastUserText});
      }
      if(data.text)typeAnswer(data.text);
      else render();
    }
  };
})();

const Settings=(function(){
  function render(){
    if(currentApp!=='settings')return;
    $('controls').classList.add('hidden');
    $('lamp').innerHTML='<div class="runtime-settings">'+
      '<div class="topbar"><span>'+timeText()+'</span><span>'+statusIcon(Runtime.state)+'</span></div>'+
      '<div class="head">'+mascot(30,true)+'<span class="head-name">设置</span><div class="head-right">设备与提醒</div></div>'+
      '<div class="card fade">'+
        '<div class="runtime-setting-row"><div><strong>语音唤醒</strong><div class="runtime-sub">说“你好灵犀”开始对话</div></div><button id="runtimeWakeToggle" class="runtime-chip '+(Runtime.wakeWordEnabled?'on':'')+'" onclick="Runtime.toggleWakeWord()">'+(Runtime.wakeWordEnabled?'已开启':'已关闭')+'</button></div>'+
        '<div class="runtime-setting-row"><div><strong>服务状态</strong><div class="runtime-sub" id="runtimeServiceText">'+escapeHtml(Runtime.stateLabel())+'</div></div><button class="runtime-chip" onclick="Runtime.reconnect()">重新连接</button></div>'+
      '</div>'+
      '<div class="card fade"><div><strong style="font-size:14px">倒计时</strong><div class="runtime-sub">输入分钟数，最多 999 分钟</div></div><div class="runtime-timer-form"><input id="runtimeTimerInput" type="number" min="1" max="999" inputmode="numeric" placeholder="分钟"><button class="primary-btn" onclick="Runtime.setTimerFromInput()">开始</button></div><div id="runtimeTimerSlot"></div></div>'+
      (Runtime.errorMessage?'<div class="runtime-error">'+escapeHtml(Runtime.errorMessage)+'</div>':'')+
      '</div>';
    const input=$('runtimeTimerInput');
    if(input)input.addEventListener('keydown',function(event){if(event.key==='Enter')Runtime.setTimerFromInput();});
    Runtime.updateTimerView();
  }
  return{start:render,stop:function(){},refresh:render,replay:render,togglePlay:function(){return false;},render:render};
})();

const localLampSetLevel=Lamp.setLevel.bind(Lamp);

const Runtime={
  ws:null,
  state:'connecting',
  wakeWordEnabled:false,
  errorMessage:'',
  reconnectDelay:1000,
  reconnectTimer:null,
  timerInterval:null,
  timerSeconds:0,
  timerLabel:'',

  init:function(){
    const self=this;
    this.enhanceHome();
    this.updateClock();
    setInterval(function(){self.updateClock();},10000);
    this.connect();
  },

  stateLabel:function(){
    return {connecting:'正在连接',disconnected:'已断开',idle:'在线',listening:'正在聆听',thinking:'正在思考',speaking:'正在播报'}[this.state]||this.state;
  },

  connect:function(){
    if(this.reconnectTimer){clearTimeout(this.reconnectTimer);this.reconnectTimer=null;}
    if(this.ws){try{this.ws.close();}catch(error){}this.ws=null;}
    this.setState('connecting');
    const protocol=location.protocol==='https:'?'wss:':'ws:';
    const self=this;
    this.ws=new WebSocket(protocol+'//'+location.host+'/ws');
    this.ws.onopen=function(){self.reconnectDelay=1000;self.errorMessage='';self.setState('connecting');};
    this.ws.onmessage=function(event){
      if(typeof event.data!=='string')return;
      try{self.handleMessage(JSON.parse(event.data));}catch(error){console.warn('[Runtime] 无法解析消息',error);}
    };
    this.ws.onclose=function(){
      self.ws=null;
      self.setState('disconnected');
      self.reconnectTimer=setTimeout(function(){
        self.reconnectDelay=Math.min(self.reconnectDelay*2,10000);
        self.connect();
      },self.reconnectDelay);
    };
    this.ws.onerror=function(){};
  },

  send:function(payload){
    if(this.ws&&this.ws.readyState===WebSocket.OPEN){this.ws.send(JSON.stringify(payload));return true;}
    return false;
  },

  reconnect:function(){
    this.errorMessage='';
    if(this.ws&&this.ws.readyState===WebSocket.OPEN)this.send({type:'reconnect'});
    else this.connect();
    this.setState('connecting');
  },

  setState:function(state){
    this.state=state;
    this.updateHomeIndicators();
    LiveDialog.render();
    if(currentApp==='settings')Settings.render();
  },

  primaryAction:function(){
    if(this.state==='connecting'||this.state==='disconnected'){this.reconnect();return;}
    if(this.state==='speaking'){
      this.send({type:'abort'});
      LiveDialog.clear();
      return;
    }
    if(this.state==='listening'||this.state==='thinking')this.send({type:'stop_listening'});
    else{
      LiveDialog.clear();
      this.send({type:'start_listening'});
    }
  },

  toggleWakeWord:function(){this.send({type:'toggle_wake_word'});},

  handleMessage:function(data){
    switch(data.type){
      case 'state':this.setState(data.state);break;
      case 'stt':if(data.text)LiveDialog.onSTT(data.text);break;
      case 'llm':break;
      case 'tts':LiveDialog.onTTS(data);break;
      case 'error':
        this.errorMessage='⚠ '+(data.message||'服务异常');
        LiveDialog.render();
        if(currentApp==='settings')Settings.render();
        break;
      case 'wake_detected':
        LiveDialog.clear();
        if(currentApp!=='dialog')openApp('dialog');
        break;
      case 'wake_word':
        this.wakeWordEnabled=!!data.enabled;
        this.updateHomeIndicators();
        LiveDialog.render();
        if(currentApp==='settings')Settings.render();
        break;
      case 'timer_start':this.startTimerDisplay(data.seconds,data.label);break;
      case 'timer_done':this.timerDone(data.label);break;
      case 'timer_cancelled':this.clearTimerDisplay();break;
      case 'light_state':localLampSetLevel(Number(data.level)||0);break;
    }
  },

  setLightLevel:function(level){
    const value=Math.max(0,Math.min(3,Number(level)||0));
    localLampSetLevel(value);
    this.send({type:'set_brightness',level:value});
  },

  setTimerFromInput:function(){
    const input=$('runtimeTimerInput');
    let minutes=input?parseInt(input.value,10):0;
    if(!minutes||minutes<1){this.errorMessage='请输入有效的分钟数';Settings.render();return;}
    minutes=Math.min(minutes,999);
    this.errorMessage='';
    this.send({type:'set_timer',minutes:minutes,label:minutes+'分钟'});
    if(input)input.value='';
  },

  cancelTimer:function(){
    this.send({type:'cancel_timer'});
    this.clearTimerDisplay();
  },

  startTimerDisplay:function(seconds,label){
    const self=this;
    this.timerSeconds=Math.max(0,Number(seconds)||0);
    this.timerLabel=label||'倒计时';
    if(this.timerInterval)clearInterval(this.timerInterval);
    this.timerInterval=setInterval(function(){
      self.timerSeconds--;
      if(self.timerSeconds<=0){self.clearTimerDisplay();return;}
      self.updateTimerView();
    },1000);
    this.updateTimerView();
  },

  clearTimerDisplay:function(){
    if(this.timerInterval)clearInterval(this.timerInterval);
    this.timerInterval=null;
    this.timerSeconds=0;
    this.timerLabel='';
    this.updateTimerView();
  },

  updateTimerView:function(){
    const slot=$('runtimeTimerSlot');
    if(!slot)return;
    if(this.timerSeconds<=0){slot.innerHTML='<div class="runtime-sub" style="text-align:center;padding-top:3px">当前没有运行中的倒计时</div>';return;}
    const minutes=Math.floor(this.timerSeconds/60);
    const seconds=this.timerSeconds%60;
    slot.innerHTML='<div class="runtime-timer-card"><i class="ti ti-alarm" style="font-size:24px;color:#2b9bf4" aria-hidden="true"></i><div class="runtime-grow"><div class="runtime-sub">'+escapeHtml(this.timerLabel)+'</div><div class="runtime-timer-value">'+minutes+':'+String(seconds).padStart(2,'0')+'</div></div><button class="runtime-chip runtime-danger" onclick="Runtime.cancelTimer()">取消</button></div>';
  },

  timerDone:function(label){
    this.clearTimerDisplay();
    alert('⏰ '+(label||'倒计时')+' 时间到！');
  },

  enhanceHome:function(){
    const tile=document.querySelector('.home-tile.home-gray');
    if(tile){tile.classList.remove('muted');tile.onclick=function(){openApp('settings');};}
    const bar=document.querySelector('.home-bar');
    if(bar){bar.classList.add('runtime-clickable');bar.title='点击切换语音唤醒';bar.onclick=function(){Runtime.toggleWakeWord();};}
    this.updateHomeIndicators();
  },

  updateClock:function(){
    document.querySelectorAll('.home-clock').forEach(function(el){el.textContent=timeText();});
    document.querySelectorAll('.home-date').forEach(function(el){el.textContent=dateText();});
    if(currentApp==='dialog'){const timeEl=$('lamp').querySelector('.topbar span:first-child');if(timeEl)timeEl.textContent=timeText();}
    if(currentApp==='settings')Settings.render();
  },

  updateHomeIndicators:function(){
    const icons=document.querySelector('.home-status .hs-icons');
    if(icons){
      const wifi=icons.querySelector('.ti-wifi');
      if(wifi)wifi.className='ti ti-wifi runtime-status '+(this.state==='connecting'?'connecting':this.state==='disconnected'?'offline':'online');
    }
    const text=document.querySelector('.home-bar-text');
    if(text)text.textContent=this.wakeWordEnabled?'说“你好灵犀”随时叫我':'语音唤醒已关闭，点击这里开启';
  }
};

window.Runtime=Runtime;
Lamp.setLevel=function(level){Runtime.setLightLevel(level);};
modules.dialog=LiveDialog;
modules.settings=Settings;

const prototypeOpenApp=openApp;
openApp=function(name){
  prototypeOpenApp(name);
  if(name==='dialog')LiveDialog.start();
  else if(name==='settings')Settings.start();
  else{
    $('voiceSel').classList.remove('hidden');
    $('controls').classList.remove('hidden');
    $('btnPlay').innerHTML='<i class="ti ti-player-pause" aria-hidden="true"></i> 暂停';
    $('btnRe').innerHTML='<i class="ti ti-refresh" aria-hidden="true"></i> 重播';
    $('btnSnd').innerHTML=Voice.on?'<i class="ti ti-volume" aria-hidden="true"></i> 关闭语音':'<i class="ti ti-volume" aria-hidden="true"></i> 开启语音';
  }
};
window.openApp=openApp;

const prototypeShowHome=showHome;
showHome=function(){prototypeShowHome();Runtime.enhanceHome();Runtime.updateClock();};
window.showHome=showHome;

$('btnPlay').onclick=function(){
  if(currentApp==='dialog'){Runtime.primaryAction();return;}
  if(currentApp==='home'||currentApp==='settings')return;
  const playing=modules[currentApp].togglePlay();
  this.innerHTML=playing?'<i class="ti ti-player-pause" aria-hidden="true"></i> 暂停':'<i class="ti ti-player-play" aria-hidden="true"></i> 播放';
};

$('btnRe').onclick=function(){
  if(currentApp==='dialog'){Runtime.reconnect();return;}
  if(currentApp==='home'||currentApp==='settings')return;
  modules[currentApp].replay();
  $('btnPlay').innerHTML='<i class="ti ti-player-pause" aria-hidden="true"></i> 暂停';
};

$('btnSnd').onclick=function(){
  if(currentApp==='dialog'){Runtime.toggleWakeWord();return;}
  if(currentApp==='home'||currentApp==='settings')return;
  Voice.on=!Voice.on;
  this.innerHTML=Voice.on?'<i class="ti ti-volume" aria-hidden="true"></i> 关闭语音':'<i class="ti ti-volume" aria-hidden="true"></i> 开启语音';
  modules[currentApp].refresh();
};

Runtime.init();
})();
