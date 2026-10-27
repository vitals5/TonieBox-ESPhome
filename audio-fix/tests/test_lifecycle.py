"""Compile the actual patched loop/command/cleanup methods against deterministic SDK fakes.
Not an ESP-IDF ABI or real multicore/hardware test.
"""
from pathlib import Path
import re
import subprocess
import tempfile

source = Path(__file__).resolve().parents[1] / 'components/i2s_audio/speaker/i2s_audio_speaker.cpp'
text = source.read_text()
header = source.with_suffix('.h').read_text()
names = ['loop', 'start', 'stop_', 'retry_start_', 'enable_i2s_channel_', 'disable_i2s_channel_', 'stop_i2s_driver_', 'init_i2s_channel_']
methods = []
for name in names:
    match = re.search(r'(?:void|esp_err_t) I2SAudioSpeakerBase::' + name + r'\([^)]*\) \{', text)
    assert match, name
    end = match.end()
    depth = 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    methods.append(text[match.start():end])
enums = re.search(r'enum SpeakerEventGroupBits.*?\n};', header, re.S)[0]
prefix = r'''
#include <cassert>
#include <cstdint>
#include <cstdio>
using EventBits_t = uint32_t;
using esp_err_t = int;
constexpr int ESP_OK=0, ESP_ERR_NO_MEM=257, MALLOC_CAP_DMA=1, MALLOC_CAP_INTERNAL=2;
struct i2s_chan_config_t {}; struct i2s_std_config_t {};
int mode_error=0, queue_calls=0, fail_queue_call=0;
int i2s_new_channel(const i2s_chan_config_t *, void **p, void *) {*p=(void *)1; return 0;}
int i2s_channel_init_std_mode(void *, const i2s_std_config_t *) {return mode_error;}
void *xQueueCreate(size_t,size_t) {return ++queue_calls==fail_queue_call ? nullptr : (void *)1;}
void xQueueReset(void *) {}
#define ESP_LOGE(...) ((void)0)
#define ESP_LOGD(...) ((void)0)
#define ESP_LOGV(...) ((void)0)
#define LOG_STR(x) x
namespace speaker { enum {STATE_STOPPED, STATE_STARTING, STATE_RUNNING, STATE_STOPPING}; }
uint32_t bits=0, clock_ms=0;
int enables=0, disables=0, deletes=0, queue_deletes=0, task_creates=0, driver_starts=0;
int enable_error=0, delete_error=0;
bool driver_failure=false, task_failure=false;
uint32_t bits_during_delete=0;
uint32_t xEventGroupGetBits(int) {return bits;}
uint32_t xEventGroupClearBits(int, uint32_t clear) {auto old=bits; bits &= ~clear; return old;}
void xEventGroupSetBits(int, uint32_t add) {bits |= add;}
void vTaskDelete(void *p) {assert(p);}
void vQueueDelete(void *) {++queue_deletes;}
int i2s_channel_enable(void *) {++enables; return enable_error;}
int i2s_channel_disable(void *) {++disables; return 0;}
int i2s_del_channel(void *) {++deletes; bits |= bits_during_delete; return delete_error;}
const char *esp_err_to_name(int) {return "fake";}
unsigned heap_caps_get_free_size(int) {return 10000;}
unsigned heap_caps_get_largest_free_block(int) {return 5000;}
void xTaskCreate(void (*)(void *), const char *, int, void *, int, void **p) {
 ++task_creates; *p=task_failure ? nullptr : (void *)1;
}
constexpr int TASK_STACK_SIZE=4096, TASK_PRIORITY=19;
struct Parent {int unlocks=0; void unlock(){++unlocks;}};
'''
klass = r'''
class I2SAudioSpeakerBase {
public:
 int event_group_=1, state_=speaker::STATE_STOPPED, audio_stream_info_=0;
 void *speaker_task_handle_=nullptr, *tx_handle_=nullptr, *i2s_event_queue_=nullptr, *write_records_queue_=nullptr;
 Parent parent; Parent *parent_=&parent;
 bool channel_enabled_=false, permanent=false;
 uint8_t start_failures_=0;
 uint32_t deadline=0;
 void loop(); void start(); void stop_(bool); void retry_start_(const char *);
 int enable_i2s_channel_(); void disable_i2s_channel_(); void stop_i2s_driver_();
 int init_i2s_channel_(const i2s_chan_config_t &,const i2s_std_config_t &,size_t);
 bool is_ready(){return true;} bool is_failed(){return false;}
 bool status_has_error(){return permanent || clock_ms < deadline;}
 void status_clear_error(){permanent=false; deadline=0;}
 void status_set_error(const char *){permanent=true;}
 void status_momentary_error(const char *, unsigned ms){deadline=clock_ms+ms;}
 void cancel_timeout(const char *){}
 void on_task_stopped(){} void park_dout_pin_(){} void post_software_gain_(int){}
 int start_i2s_driver(int &){++driver_starts; if(driver_failure)return 1; tx_handle_=(void *)1; return 0;}
 static void speaker_task(void *){}
};
'''
tests = r'''
int main() {
 // Start then stop cancels unprocessed work. Stop then start preserves newer work.
 {I2SAudioSpeakerBase s; bits=0; s.start(); s.stop_(false); s.loop(); assert(s.state_==speaker::STATE_STOPPED);}
 {I2SAudioSpeakerBase s; bits=0; s.stop_(false); s.start(); s.loop(); assert(s.speaker_task_handle_);}
 // A start posted while a running task has a pending stop survives the cleanup handoff.
 {I2SAudioSpeakerBase s; bits=0; s.state_=speaker::STATE_RUNNING; s.speaker_task_handle_=(void *)1;
 s.stop_(false); s.start(); assert(bits & COMMAND_START);
 bits=TASK_STOPPED|COMMAND_START; s.loop(); assert(bits & COMMAND_START);
 s.loop(); assert(s.speaker_task_handle_);}
 // No channel disable before enable, exactly one disable after enable, including failed enable.
 {I2SAudioSpeakerBase s; int before=disables; s.tx_handle_=(void *)1; s.stop_i2s_driver_(); assert(disables==before);
 s.tx_handle_=(void *)1; enable_error=1; s.enable_i2s_channel_(); s.stop_i2s_driver_(); assert(disables==before);
 s.tx_handle_=(void *)1; enable_error=0; s.enable_i2s_channel_(); s.stop_i2s_driver_(); assert(disables==before+1);}
 // Driver failures back off at 250/500/1000 ms, then latch; repeated play cannot evade the limit.
 {I2SAudioSpeakerBase s; bits=COMMAND_START; clock_ms=0; driver_failure=true;
 int before=driver_starts;
 for(unsigned delay: {250u,500u,1000u}) {s.loop(); assert(s.deadline==clock_ms+delay);
 s.loop(); assert(driver_starts==before+int(s.start_failures_)); clock_ms+=delay;}
 s.loop(); assert(s.permanent && s.start_failures_==4);
 for(int i=0;i<100;i++){s.start();s.loop();} assert(driver_starts==before+4);
 s.stop_(false); s.loop(); assert(!s.permanent && s.start_failures_==0); driver_failure=false;}
 // A failed setup may post its error a loop pass before TASK_STOPPED: do not consume it early.
 {I2SAudioSpeakerBase s; bits=ERR_ESP_NO_MEM; s.state_=speaker::STATE_STARTING; s.speaker_task_handle_=(void *)1;
 s.loop(); assert(bits & ERR_ESP_NO_MEM); bits|=TASK_STOPPED; s.loop(); assert(s.start_failures_==1 && s.status_has_error());}
 // Do not launch another task while the old one still exists.
 {I2SAudioSpeakerBase s; bits=0; s.state_=speaker::STATE_STARTING; s.speaker_task_handle_=(void *)1;
 int before=task_creates; s.loop(); assert(task_creates==before);}
 // Release both retained queues only after channel deletion succeeds.
 {I2SAudioSpeakerBase s; s.tx_handle_=(void *)1; s.i2s_event_queue_=(void *)1; s.write_records_queue_=(void *)1;
 int before=queue_deletes; delete_error=1; s.stop_i2s_driver_(); assert(s.tx_handle_ && queue_deletes==before);
 delete_error=0; s.stop_i2s_driver_(); assert(!s.tx_handle_ && queue_deletes==before+2);}
 // A stop arriving during cleanup cancels retry, including a previously exhausted budget.
 {I2SAudioSpeakerBase s; bits=TASK_STOPPED|ERR_ESP_NO_MEM; s.start_failures_=3;
 s.tx_handle_=(void *)1; bits_during_delete=COMMAND_STOP; s.loop(); bits_during_delete=0;
 assert(s.start_failures_==0 && !s.permanent && !(bits & COMMAND_START));}
 // Failed task setup retries without depending on another producer play call.
 {I2SAudioSpeakerBase s; bits=TASK_STOPPED|ERR_ESP_NO_MEM; s.loop();
 assert(s.state_==speaker::STATE_STARTING); int before=task_creates; s.loop(); assert(task_creates==before);
 clock_ms=s.deadline; s.loop(); assert(task_creates==before+1);}
 // Partial queue allocation releases the channel and whichever queue succeeded.
 {for(int fail: {1,2}) {I2SAudioSpeakerBase s; queue_calls=0; fail_queue_call=fail;
 int before=queue_deletes; assert(s.init_i2s_channel_({}, {}, 4)==ESP_ERR_NO_MEM);
 assert(!s.tx_handle_ && !s.i2s_event_queue_ && !s.write_records_queue_);
 assert(queue_deletes==before+1 && s.parent.unlocks==1);} fail_queue_call=0;}
 // Failed channel initialization must retain ownership when channel deletion also fails.
 {I2SAudioSpeakerBase s; mode_error=1; delete_error=1;
 assert(s.init_i2s_channel_({}, {}, 4)==1); assert(s.tx_handle_ && s.parent.unlocks==0);
 delete_error=0; s.stop_i2s_driver_(); assert(!s.tx_handle_ && s.parent.unlocks==1); mode_error=0;}
 // A retained channel after task cleanup must not generate an unbounded delete loop.
 {I2SAudioSpeakerBase s; bits=TASK_STOPPED; s.tx_handle_=(void *)1; delete_error=1;
 s.loop(); int before=deletes; for(int i=0;i<100;i++)s.loop(); assert(deletes==before);
 s.stop_(false); delete_error=0; s.loop(); assert(!s.tx_handle_);}
 // Partial startup retries channel cleanup before creating another driver.
 {I2SAudioSpeakerBase s; bits=0; s.state_=speaker::STATE_STARTING; s.tx_handle_=(void *)1;
 delete_error=1; int before=driver_starts; s.loop(); assert(s.permanent && driver_starts==before);
 s.stop_(false); s.start(); delete_error=0; s.loop(); assert(driver_starts==before+1);}
 // Stop alone also releases a retained partial-start channel during backoff.
 {I2SAudioSpeakerBase s; bits=0; s.state_=speaker::STATE_STARTING; s.tx_handle_=(void *)1;
 s.deadline=clock_ms+1000; s.stop_(false); s.loop();
 assert(!s.tx_handle_ && s.state_==speaker::STATE_STOPPED && s.parent.unlocks==1);}
 puts("15 lifecycle scenarios passed");
}
'''
with tempfile.TemporaryDirectory() as d:
    cpp = Path(d)/'test.cpp'
    cpp.write_text(prefix.replace('#include <cstdio>', '#include <cstdio>\n#include <initializer_list>') + enums + klass + '\n'.join(methods) + tests)
    binary = Path(d)/'test'
    subprocess.run(['g++','-std=c++20','-Wall','-Wextra','-Werror','-Wno-unused-parameter',str(cpp),'-o',str(binary)],check=True)
    subprocess.run([str(binary)],check=True)
