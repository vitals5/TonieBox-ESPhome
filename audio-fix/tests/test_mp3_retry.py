"""Compile actual decoder methods with deterministic codec/heap/clock/source fakes.
Covers retry policy through decode(), not ESP-IDF ABI or real task scheduling.
"""
from pathlib import Path
import re
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'components/audio/audio_decoder.cpp').read_text()
header = (root / 'components/audio/audio_decoder.h').read_text()
methods = []
for result, name in [('esp_err_t', 'start'), ('AudioDecoderState', 'decode'), ('FileDecoderState', 'decode_mp3_')]:
    match = re.search(result + r' AudioDecoder::' + name + r'\([^)]*\) \{', source)
    assert match, name
    end, depth = match.end(), 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    methods.append(source[match.start():end])
enums = '\n'.join(re.findall(r'enum class (?:AudioDecoderState|FileDecoderState).*?\n};', header, re.S))
retry_fields = '\n'.join(re.findall(r'  (?:uint8_t|uint32_t) mp3_\w+_\{0\};', header))
assert len(retry_fields.splitlines()) == 3
prefix = r'''
#include <algorithm>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <memory>
#include <optional>
#include <vector>
#define USE_AUDIO_MP3_SUPPORT
#define ESP_LOGD(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
#define pdMS_TO_TICKS(x) (x)
using esp_err_t = int;
using std::make_unique;
constexpr int ESP_OK=0, ESP_ERR_NO_MEM=1, ESP_ERR_NOT_SUPPORTED=2;
constexpr uint32_t READ_WRITE_TIMEOUT_MS=20, MAX_NO_OUTPUT_ITERATIONS=32, MAX_POTENTIALLY_FAILED_COUNT=10;
uint32_t clock_ms=0;
unsigned max_delay=0, delays=0;
uint32_t millis(){return clock_ms;}
void delay(unsigned ms){max_delay=std::max(max_delay,ms); ++delays;}
namespace audio {
struct AudioStreamInfo {
 AudioStreamInfo(int,int,int){}
 size_t frames_to_bytes(size_t n){return n*2;}
 size_t bytes_to_frames(size_t n){return n/2;}
 uint32_t frames_to_milliseconds_with_remainder(uint32_t *n){*n=0; return 0;}
};
}
namespace micro_mp3 {
enum Mp3Result {MP3_OK=0, MP3_NEED_MORE_DATA=1, MP3_STREAM_INFO_READY=2, MP3_INPUT_INVALID=-1,
 MP3_ALLOCATION_FAILED=-2, MP3_OUTPUT_BUFFER_TOO_SMALL=-3, MP3_DECODE_ERROR=-4, MP3_STREAM_INFO_CHANGED=-5};
struct Mp3Decoder {
 bool initialized=false;
 unsigned calls=0;
 std::vector<Mp3Result> results;
 Mp3Result decode(const uint8_t *,size_t,uint8_t *,size_t,size_t &consumed,size_t &samples){
  assert(calls<results.size()); auto result=results[calls++]; consumed=0; samples=0;
  if(result>=0){initialized=true; if(result==MP3_STREAM_INFO_READY)consumed=3;}
  return result;
 }
 bool is_initialized(){return initialized;}
 int get_channels(){return 1;} int get_sample_rate(){return 44100;}
 int get_samples_per_frame(){return 1152;} int get_min_output_buffer_bytes(){return 4608;}
};
}
enum class AudioFileType {NONE,MP3};
struct Source {
 uint8_t data_[64]{}; size_t remaining=64, consumed=0;
 const uint8_t *data(){return data_+consumed;}
 size_t available(){return remaining;}
 bool has_buffered_data(){return remaining>0;}
 void fill(unsigned,bool){}
 void consume(size_t n){assert(n<=remaining); consumed+=n; remaining-=n;}
};
struct Sink {
 uint8_t data_[4608]{}; size_t used=0; bool allocation_ok=true;
 uint8_t *get_buffer_end(){return data_+used;}
 size_t free(){return sizeof(data_)-used;} size_t capacity(){return sizeof(data_);}
 size_t available(){return used;}
 size_t transfer_data_to_sink(unsigned,bool){auto n=used; used=0; return n;}
 void increase_buffer_length(size_t n){used+=n;}
 bool reallocate(size_t){return allocation_ok;}
};
'''
klass = r'''
class AudioDecoder {
public:
 esp_err_t start(AudioFileType);
 AudioDecoderState decode(bool);
 FileDecoderState decode_mp3_();
 std::unique_ptr<micro_mp3::Mp3Decoder> mp3_decoder_;
 std::unique_ptr<Source> input_buffer_=make_unique<Source>();
 std::unique_ptr<Sink> output_transfer_buffer_=make_unique<Sink>();
 AudioFileType audio_file_type_=AudioFileType::MP3;
 std::optional<audio::AudioStreamInfo> audio_stream_info_;
 size_t free_buffer_required_=0;
 uint32_t potentially_failed_count_=0, accumulated_frames_written_=0, playback_ms_=0;
 bool end_of_file_=false, pause_output_=false;
'''
tests = r'''
int main(){
 // Public decode() waits at 250/500/1000 ms, preserves bytes, and terminates after four failures.
 {AudioDecoder d; d.start(AudioFileType::MP3); d.mp3_decoder_->results.assign(4,micro_mp3::MP3_ALLOCATION_FAILED);
 clock_ms=100; for(unsigned wait: {250u,500u,1000u}){
  assert(d.decode(false)==AudioDecoderState::DECODING); assert(d.mp3_retry_delay_ms_==wait);
  unsigned calls=d.mp3_decoder_->calls; clock_ms+=wait-1;
  for(int i=0;i<50;i++)assert(d.decode(false)==AudioDecoderState::DECODING);
  assert(d.mp3_decoder_->calls==calls && d.input_buffer_->consumed==0); ++clock_ms;
 }
 assert(d.decode(false)==AudioDecoderState::FAILED); assert(d.mp3_decoder_->calls==4);
 for(int i=0;i<50;i++)assert(d.decode(false)==AudioDecoderState::FAILED);
 assert(d.mp3_decoder_->calls==4 && d.input_buffer_->consumed==0);}
 // Recovery uses the same codec and input, consuming the header exactly once.
 {AudioDecoder d; d.start(AudioFileType::MP3); auto *codec=d.mp3_decoder_.get();
 codec->results={micro_mp3::MP3_ALLOCATION_FAILED,micro_mp3::MP3_STREAM_INFO_READY};
 assert(d.decode_mp3_()==FileDecoderState::IDLE); clock_ms+=250;
 assert(d.decode_mp3_()==FileDecoderState::MORE_TO_PROCESS);
 assert(d.mp3_decoder_.get()==codec && d.input_buffer_->consumed==3 && d.audio_stream_info_.has_value());
 assert(d.mp3_start_failures_==0);}
 // Elapsed-time comparison works across millis() rollover.
 {AudioDecoder d; d.start(AudioFileType::MP3); clock_ms=UINT32_MAX-100;
 d.mp3_decoder_->results={micro_mp3::MP3_ALLOCATION_FAILED,micro_mp3::MP3_NEED_MORE_DATA};
 assert(d.decode_mp3_()==FileDecoderState::IDLE); clock_ms+=249;
 assert(d.decode_mp3_()==FileDecoderState::IDLE && d.mp3_decoder_->calls==1); ++clock_ms;
 d.decode_mp3_(); assert(d.mp3_decoder_->calls==2);}
 // A new start gets a new codec and fresh retry budget, with no deferred callbacks from the old track.
 {AudioDecoder d; d.start(AudioFileType::MP3); d.mp3_start_failures_=4; d.mp3_retry_delay_ms_=1000;
 d.start(AudioFileType::MP3); assert(d.mp3_start_failures_==0 && d.mp3_retry_delay_ms_==0);
 d.mp3_decoder_->results={micro_mp3::MP3_STREAM_INFO_READY};
 assert(d.decode_mp3_()==FileDecoderState::MORE_TO_PROCESS);}
 // Invalid data is immediately fatal; damaged frames retain their existing recoverable path.
 {AudioDecoder d; d.start(AudioFileType::MP3); d.mp3_decoder_->results={micro_mp3::MP3_INPUT_INVALID};
 assert(d.decode(false)==AudioDecoderState::FAILED && d.mp3_retry_delay_ms_==0);}
 {AudioDecoder d; d.start(AudioFileType::MP3); d.mp3_decoder_->results={micro_mp3::MP3_DECODE_ERROR};
 assert(d.decode_mp3_()==FileDecoderState::POTENTIALLY_FAILED && d.mp3_start_failures_==0);}
 // Errors after stream information is available cannot replay the initialization/header path.
 {AudioDecoder d; d.start(AudioFileType::MP3); d.audio_stream_info_.emplace(16,1,44100);
 d.mp3_decoder_->results={micro_mp3::MP3_ALLOCATION_FAILED};
 assert(d.decode_mp3_()==FileDecoderState::FAILED && d.mp3_start_failures_==0);}
 // Small output buffers keep the original resizing/error behavior.
 {AudioDecoder d; d.start(AudioFileType::MP3); d.mp3_decoder_->results={micro_mp3::MP3_OUTPUT_BUFFER_TOO_SMALL};
 d.output_transfer_buffer_->allocation_ok=false;
 assert(d.decode_mp3_()==FileDecoderState::FAILED && d.mp3_retry_delay_ms_==0);}
 // Backoff yields in <=20 ms steps through the public decoder; it never sleeps for 250-1000 ms.
 assert(delays>0 && max_delay<=READ_WRITE_TIMEOUT_MS);
 puts("9 MP3 retry scenarios passed");
}
'''
with tempfile.TemporaryDirectory() as tmp:
    cpp = Path(tmp) / 'test.cpp'
    cpp.write_text(prefix + enums + klass + retry_fields + '\n};\n' + '\n'.join(methods) + tests)
    binary = Path(tmp) / 'test'
    subprocess.run(['g++', '-std=c++20', '-Wall', '-Wextra', '-Werror', str(cpp), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
