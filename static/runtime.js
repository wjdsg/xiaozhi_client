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
  if(Runtime.state==='thinking'||Runtime.state==='speaking')right='<div class="wvb" style="height:10px;width:4px"></div><div class="wvb" style="height:15px;width:4px;animation-delay:.2s"></div><div class="wvb" style="height:11px;width:4px;animation-delay:.4s"></div>';
  return '<div class="topbar"><span>'+timeText()+'</span><span>'+statusIcon(Runtime.state)+'</span></div>'+
    '<div class="head"><span class="head-mascot" onclick="goHome()" title="返回主屏">'+mascot(30,true)+'</span><span class="head-name">灵犀</span><div class="head-right">'+right+'</div></div>';
};

const LiveDialog=(function(){
  let messages=[];
  let lastUserText='';
  let typeTimer=null;
  let messageSequence=0;
  let activeAssistantTurn=null;

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
      return '<div class="runtime-center fade">'+mascot(112)+'<div class="runtime-title">我在</div><div style="display:flex;gap:5px;height:22px;align-items:center">'+waves+'</div>'+activityHtml('listening','正在聆听…')+'</div>';
    }
    if(Runtime.state==='connecting')return '<div class="runtime-center fade">'+mascot(100)+'<div class="runtime-title">正在连接</div><div class="runtime-sub">正在准备语音服务…</div></div>';
    if(Runtime.state==='disconnected'){
      const connectionFailed=Runtime.sessionEndReason==='connection_lost';
      const title=connectionFailed?'暂时无法连接':'本次对话已结束';
      const subtitle=Runtime.errorMessage||(connectionFailed?'请检查网络后继续对话':'点击下方“继续对话”重新开始');
      return '<div class="runtime-center fade">'+mascot(100,true)+'<div class="runtime-title">'+title+'</div><div class="runtime-sub">'+escapeHtml(subtitle)+'</div></div>';
    }
    const waves=[12,20,16,22,13].map(function(h,i){return '<div class="wvb" style="height:'+h+'px;animation-delay:'+(i*.15)+'s"></div>';}).join('');
    return '<div class="runtime-center fade">'+mascot(112)+'<div class="runtime-title">我在</div><div style="display:flex;gap:5px;height:22px;align-items:center">'+waves+'</div><div class="runtime-wait-cursor" aria-hidden="true"></div></div>';
  }

  function activityHtml(kind,text){
    if(kind==='listening')return '<div class="runtime-activity listening"><span class="runtime-activity-dot"></span><span>'+text+'</span></div>';
    return '<div class="runtime-activity responding"><span class="runtime-activity-bars" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i></span><span>'+text+'</span></div>';
  }


  function dialogActionsHtml(){
    if(Runtime.state==='connecting')return '';
    const label=Runtime.state==='disconnected'
      ?'<i class="ti ti-message-circle" aria-hidden="true"></i> 继续对话'
      :Runtime.state==='speaking'
        ?'<i class="ti ti-player-stop" aria-hidden="true"></i> 打断'
        :'<i class="ti ti-logout" aria-hidden="true"></i> 结束对话';
    return '<div class="runtime-dialog-actions"><button class="runtime-dialog-primary" onclick="Runtime.primaryAction()">'+label+'</button></div>';
  }
  function render(){
    if(currentApp!=='dialog')return;
    let body;
    if((Runtime.state==='listening'&&!messages.length)||(!messages.length&&Runtime.state!=='thinking'&&Runtime.state!=='speaking')){
      body=centerHtml();
    }else{
      const responding=Runtime.state==='thinking'||Runtime.state==='speaking';
      const activity=Runtime.state==='listening'?activityHtml('listening','正在聆听…'):responding?activityHtml('responding','正在回答…'):'';
      body='<div id="runtimeChat" class="runtime-chat fade">'+chatHtml()+'</div>'+activity+(responding?'<div class="glowbar"></div>':'');
    }
    const actions=dialogActionsHtml();
    $('lamp').innerHTML='<div class="screen-page runtime-dialog-page">'+dialogHeader()+body+actions+'</div>';
    const chat=$('runtimeChat');
    if(chat)chat.scrollTop=chat.scrollHeight;
    updateControls();
  }

  function updateControls(){
    if(currentApp!=='dialog')return;
    const btn=$('btnPlay');
    const labels={connecting:'正在准备…',disconnected:'继续对话',idle:'结束对话',listening:'结束对话',thinking:'结束对话',speaking:'打断'};
    btn.innerHTML=labels[Runtime.state]||labels.idle;
    btn.disabled=Runtime.state==='connecting';
    $('btnRe').innerHTML='<i class="ti ti-refresh" aria-hidden="true"></i> 重连';
    $('btnSnd').innerHTML='<i class="ti ti-bell-ringing" aria-hidden="true"></i> 唤醒词'+(Runtime.wakeWordEnabled?'开':'关');
    $('voiceSel').classList.add('hidden');
    $('controls').classList.add('hidden');
  }

  function updateUser(data){
    const text=typeof data==='string'?data:(data&&data.text)||'';
    if(!text)return;
    const turnId=data&&data.turn_id!=null?'user-'+String(data.turn_id):'user-local-'+(++messageSequence);
    lastUserText=text;
    const last=messages[messages.length-1];
    if(last&&last.role==='user'&&last.turnId===turnId)last.text=text;
    else messages.push({role:'user',text:text,turnId:turnId});
    render();
  }

  function typeAnswer(data){
    const text=typeof data==='string'?data:(data&&data.text)||'';
    if(!text){render();return;}
    const turnId=data&&data.turn_id!=null?'assistant-'+String(data.turn_id):(activeAssistantTurn||'assistant-local-'+(++messageSequence));
    activeAssistantTurn=turnId;
    let answer=null;
    for(let i=messages.length-1;i>=0;i--){
      if(messages[i].role==='assistant'&&messages[i].turnId===turnId){answer=messages[i];break;}
    }
    if(!answer){
      answer={role:'assistant',text:'',targetText:'',segments:[],turnId:turnId};
      messages.push(answer);
    }
    if(!answer.segments)answer.segments=[];
    if(answer.segments.indexOf(text)!==-1){render();return;}
    answer.segments.push(text);
    answer.targetText=(answer.targetText||answer.text||'')+text;
    clearTypeTimer();
    render();
    typeTimer=setInterval(function(){
      if(answer.text.length>=answer.targetText.length){clearTypeTimer();return;}
      answer.text=answer.targetText.slice(0,answer.text.length+1);
      const chat=$('runtimeChat');
      if(chat){
        const bubbles=chat.querySelectorAll('.ab');
        for(let i=bubbles.length-1;i>=0;i--){
          const item=messages.filter(function(message){return message.role==='assistant';})[i];
          if(item===answer){bubbles[i].textContent=answer.text;break;}
        }
        chat.scrollTop=chat.scrollHeight;
      }
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
    clear:function(){clearTypeTimer();messages=[];lastUserText='';activeAssistantTurn=null;render();},
    onWakeGreeting:function(text){
      clearTypeTimer();
      const greeting=text||"\u4f60\u597d\uff0c\u6211\u5728";
      const turnId='wake-'+(++messageSequence);
      messages.push({role:'assistant',text:greeting,targetText:greeting,segments:[greeting],turnId:turnId});
      activeAssistantTurn=null;
      render();
    },
    onSTT:updateUser,
    onTTS:function(data){
      data=data||{};
      if(data.state==='start')activeAssistantTurn=data.turn_id!=null?'assistant-'+String(data.turn_id):'assistant-local-'+(++messageSequence);
      if(data.text)typeAnswer(data);
      else render();
      if(data.state==='stop')activeAssistantTurn=null;
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
        '<div class="runtime-setting-row"><div><strong>\u8bed\u97f3\u5524\u9192</strong><div class="runtime-sub">\u8bf4\u201c\u4f60\u597d\u7075\u7280\u201d\u5f00\u59cb\u5bf9\u8bdd</div></div><button id="runtimeWakeToggle" class="runtime-chip '+(Runtime.wakeWordPending?'pending':Runtime.wakeWordEnabled?'on':'')+'" onclick="Runtime.toggleWakeWord()" '+(Runtime.wakeWordPending?'disabled':'')+'>'+(Runtime.wakeWordPending?(Runtime.wakeWordPendingTarget?'\u6b63\u5728\u5f00\u542f\u2026':'\u6b63\u5728\u5173\u95ed\u2026'):(Runtime.wakeWordEnabled?'\u5df2\u5f00\u542f':'\u5df2\u5173\u95ed'))+'</button></div>'+
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
  wakeWordPending:false,
  wakeWordPendingTarget:null,
  wakeWordRequestTimer:null,
  errorMessage:'',
  reconnectDelay:1000,
  reconnectTimer:null,
  timerInterval:null,
  timerSeconds:0,
  timerLabel:'',
  pendingDialogStart:false,
  sessionEndReason:'',

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
    this.sessionEndReason='';
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

  openDialog:function(source){
    const fromWake=source==='wake';
    this.pendingDialogStart=!fromWake;
    if(!fromWake){
      LiveDialog.clear();
      this.errorMessage='';
      this.sessionEndReason='';
    }
    openApp('dialog');
    if(!fromWake)this.startDialogListening();
  },

  startDialogListening:function(){
    if(currentApp!=='dialog'||!this.pendingDialogStart)return;
    if(this.state==='disconnected'){this.reconnect();return;}
    if(this.state==='connecting'||!this.ws||this.ws.readyState!==WebSocket.OPEN)return;
    if(this.state==='idle'){
      if(this.send({type:'start_listening'}))this.pendingDialogStart=false;
      return;
    }
    this.pendingDialogStart=false;
  },

  primaryAction:function(){
    if(this.state==='connecting')return;
    if(this.state==='disconnected'){
      this.pendingDialogStart=true;
      LiveDialog.clear();
      this.reconnect();
      return;
    }
    if(this.state==='speaking'){
      this.send({type:'abort'});
      return;
    }
    if(this.state==='listening'||this.state==='thinking'||this.state==='idle'){
      this.pendingDialogStart=false;
      this.send({type:'end_conversation'});
    }
  },

  toggleWakeWord:function(){
    if(this.wakeWordPending)return false;
    if(!this.ws||this.ws.readyState!==WebSocket.OPEN){
      this.errorMessage="\u8bf7\u5148\u8fde\u63a5\u8bed\u97f3\u670d\u52a1";
      LiveDialog.render();
      if(currentApp==='settings')Settings.render();
      return false;
    }
    const target=!this.wakeWordEnabled;
    this.wakeWordPending=true;
    this.wakeWordPendingTarget=target;
    this.errorMessage="";
    this.updateHomeIndicators();
    LiveDialog.render();
    if(currentApp==='settings')Settings.render();
    this.send({type:"toggle_wake_word"});
    if(this.wakeWordRequestTimer)clearTimeout(this.wakeWordRequestTimer);
    const self=this;
    this.wakeWordRequestTimer=setTimeout(function(){
      if(!self.wakeWordPending)return;
      self.wakeWordPending=false;
      self.wakeWordPendingTarget=null;
      self.errorMessage="\u5524\u9192\u8bcd\u64cd\u4f5c\u8d85\u65f6\uff0c\u8bf7\u68c0\u67e5\u670d\u52a1\u8fde\u63a5";
      self.updateHomeIndicators();
      LiveDialog.render();
      if(currentApp==='settings')Settings.render();
    },10000);
    return true;
  },
  handleMessage:function(data){
    switch(data.type){
      case 'session_end':
        this.sessionEndReason=data.reason||'session_ended';
        this.errorMessage=data.message||"\u672c\u6b21\u5bf9\u8bdd\u5df2\u7ed3\u675f\uff0c\u53ef\u4ee5\u70b9\u51fb\u7ee7\u7eed\u5bf9\u8bdd";
        LiveDialog.clear();
        this.setState('disconnected');
        break;
      case 'state':
        if(data.state==='idle'||data.state==='listening'){
          this.errorMessage='';
          this.sessionEndReason='';
        }
        this.setState(data.state);
        if(data.state==='idle'&&this.pendingDialogStart)this.startDialogListening();
        break;
      case 'stt':if(data.text)LiveDialog.onSTT(data);break;
      case 'llm':break;
      case 'tts':LiveDialog.onTTS(data);break;
      case 'error':
        this.errorMessage='⚠ '+(data.message||'服务异常');
        LiveDialog.render();
        if(currentApp==='settings')Settings.render();
        break;
      case 'wake_detected':
        if(currentApp!=='dialog')this.openDialog('wake');
        else this.pendingDialogStart=false;
        if(this.state==='disconnected'){
          this.errorMessage='';
          this.sessionEndReason='';
          this.setState('connecting');
        }
        LiveDialog.onWakeGreeting("\u4f60\u597d\uff0c\u6211\u5728");
        break;
      case 'wake_word':
        const requested=this.wakeWordPendingTarget;
        if(this.wakeWordRequestTimer)clearTimeout(this.wakeWordRequestTimer);
        this.wakeWordRequestTimer=null;
        this.wakeWordPending=false;
        this.wakeWordPendingTarget=null;
        this.wakeWordEnabled=!!data.enabled;
        if(requested!==null&&this.wakeWordEnabled!==requested)this.errorMessage="\u5524\u9192\u8bcd\u64cd\u4f5c\u5931\u8d25\uff0c\u8bf7\u68c0\u67e5\u6a21\u578b\u6216\u9ea6\u514b\u98ce";
        else if(requested!==null)this.errorMessage="";
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
    if(text)text.textContent=this.wakeWordPending?(this.wakeWordPendingTarget?"\u6b63\u5728\u5f00\u542f\u8bed\u97f3\u5524\u9192\u2026":"\u6b63\u5728\u5173\u95ed\u8bed\u97f3\u5524\u9192\u2026"):this.wakeWordEnabled?"\u8bf4\u201c\u4f60\u597d\u7075\u7280\u201d\u968f\u65f6\u53eb\u6211":"\u8bed\u97f3\u5524\u9192\u5df2\u5173\u95ed\uff0c\u70b9\u51fb\u8fd9\u91cc\u5f00\u542f";
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
showHome=function(){
  if(currentApp==='dialog'&&Runtime.state!=='disconnected'){
    Runtime.pendingDialogStart=false;
    Runtime.send({type:'end_conversation'});
  }
  prototypeShowHome();
  Runtime.enhanceHome();
  Runtime.updateClock();
};
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
  if(currentApp==='dialog')return;
  if(currentApp==='home'||currentApp==='settings')return;
  Voice.on=!Voice.on;
  this.innerHTML=Voice.on?'<i class="ti ti-volume" aria-hidden="true"></i> 关闭语音':'<i class="ti ti-volume" aria-hidden="true"></i> 开启语音';
  modules[currentApp].refresh();
};

Runtime.init();
})();
