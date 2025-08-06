---
title: "基于ASIO实现C++网络库"
description: "基于ASIO实现C++网络库"
publishDate: "20 November 2019"
tags: ['C++', 'ASIO', '网络库']
---

项目里的网络库用了asio，看了asio的文档后感觉用asio挺好用的，很容易实现一个网络库，而且有单独的版本可以不依赖boost，我们的目标是实现一个多线程，每个线程都有一个事件循环的异步网络库。

<!-- more -->

## 功能划分

整个网络库划分为Acceptor，Loop，LoopMgr，Session，TcpServer这几个类。
Loop使用io_context来作为eventloop使用，post函数用来向loop中投递任务，可以看做一个任务队列，线程安全，调用run函数后可以处理各种异步事件和任务。把一些跨线程的操作投递到一个loop中执行就避免了加锁。
LoopMgr相当于一个Loop线程池。
Acceptor持有asio的acceptor用来接收新连接，
Session持有socket，使用async系列函数进行异步读写操作。
TcpServer管理连接。使用的时候通过注册TcpServer的onconnect，onmessage，ondisconnect回调函数完成对应事件的逻辑处理。
基本使用方法：
```c++
TcpServer server;
server.setNewSessionCallback<UserSession>();
server.setConnectionCallback();
server.setMessageCallback();
server.setServerDisconnectCallback();
server.run();
```

### Acceptor

Acceptor类直接封装asio的acceptor
```c++
void Acceptor::accept() {
    auto session = newSessionCallback_();
    acceptor_.async_accept(session->Socket(), [session, this](const asio::error_code &err) {
        if (!err) {
            session->start();
        } else {
            session->close();
        }
        if(!acceptor_.is_open()){        
            return ;
        }
        accept();
    });
}
```
asio的异步函数都是这种用法，需要在回调函数中重新调用。Accepter需要设置好newSessionCallback回调用来创建新的session 

### Session

Session封装了socket的读写操作，在start函数中调用设置的connectioncallback之后开始read操作
```c++
void Session::read() {
    auto self(shared_from_this());
    socket_.async_read_some(asio::buffer(read_buf_.beginWrite(), read_buf_.writableBytes()), [self, this](const asio::error_code &err, size_t size) {
        if (!err) {
            //LOG_INFO("receive data,size:{}",size);
            read_buf_.hasWritten(size);
            messagecallback_(self, &read_buf_);
            read_buf_.ensureWritableBytes(size);
            read();
        } else {
            LOG_ERROR("read error: {}", err.message());
            if(reconnect_){
                reconnect();
            }else{
                disconnectcallback_(id_);
                close();
            }
            return;
        }
    });
}
```
在回调中调用负责解包的messagecallback，这里把解包操作完全交个用户设置的回调，用户需要在回调中操作buffer，读出数据后调用retrive移动读指针，一般写法是再加一层codec负责解包后再处理消息。
```c++
void Session::send(BufferPtr buffer) {
    {
        std::lock_guard<std::mutex> guard(mutex_);
        unsend_queue_.push_back(buffer);
        // TODO(shenyu): race condition?
        if (write_buf_.readableBytes() != 0) return;
    }

    auto self(shared_from_this());
    loop_->runInLoop([this, self]() {
        bool write_in_progress = (write_buf_.readableBytes() != 0);
        if (!write_in_progress) {
            write();
        }
    });
}

void Session::write() {
    {
        std::lock_guard<std::mutex> guard(mutex_);
        tmp_queue_.swap(unsend_queue_);
        unsend_queue_.clear();
        if (tmp_queue_.empty()) return;
    }

    for (const auto &buf : tmp_queue_) {
        write_buf_.append(buf->peek(), buf->readableBytes());
    }

    auto self(shared_from_this());
    socket_.async_write_some(asio::buffer(write_buf_.peek(), write_buf_.readableBytes()), [self, this](const asio::error_code &err, size_t size) {
        if (!err) {
            write_buf_.retrieve(write_buf_.readableBytes());
            {
                std::lock_guard<std::mutex> guard(this->mutex_);
                if (this->unsend_queue_.empty()) return;
            }
            write();
        } else {
            disconnectcallback_(id_);
            LOG_ERROR("write error: {}", err.message());
            close();
            return;
        }
    });
}
```
写操作稍微复杂一些，不能直接调用异步写操作，如果连续两次调用write函数，而第一个发的包比较大，第二次的包比较小，可能第一个包没有发完第二个就发出去了，保证不了顺序。正确的做法是增加一个待发送队列，send时统一push到未发送队列，并判断发送buf是否为空，空说明当前没有在发消息，调用write，write时将未发送队列的内容拷贝到发送buf中一起发送。

### TcpServer

用户直接使用TcpServer类，只需实现事件的回调函数即可。

```c++
template <typename T>
SessionPtr TcpServer::newSession() {
    auto loop = loopmgr_.findNextLoop();
    auto session = std::make_shared<T>(loop, sessionid_++);
    session->setMessageCallback(messagecallback_);
    session->setConnectionCallback(connectioncallback_);
    session->setDisconnectCallback(std::bind(&TcpServer::DefaultDisconnectCallback, this, _1));
    {
        std::lock_guard<std::mutex> guard(mutex_);
        connections_.insert({sessionid_, session});
    }
    return session;
}
```

这里的newSession使用模板函数用来创建Session不同的子类，直接使用轮询将session派发到不同的eventloop中。

直接使用asio比用epoll还是方便不少的，而且跨平台，但是asio的代码可读性太差，不知道会不会有什么坑，性能比封装epoll肯定也要差些。

> 代码地址：https://github.com/Nickqiaoo/cppim/tree/master/net