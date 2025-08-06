---
title: "云风coroutine源码分析"
description: "云风coroutine源码分析"
publishDate: "29 October 2018"
tags: ['coroutine', '协程']
---

## 前言

现在C++的开发开始流行使用coroutine，也就是协程。我看腾讯的几个开源项目里面都有协程的实现。使用协程可以用同步的写法，达到异步的性能。它的基本原理其实就是在IO等待的时候切换出去，在适当的时刻再切换回来。[云风](https://blog.codingnow.com/2012/07/c_coroutine.html)用200行代码实现了一个最简单的协程，我们先看这个代码了解一下协程的原理，然后再看微信的[libco](https://github.com/Tencent/libco)实现。

<!-- more -->

##协程简单介绍

协程可以理解为一个用户级的线程，一个线程里跑多个协程。协程分为**对称协程**和**非对称协程**，对称协程就是当协程切换的时候他可以切换到任意其他的协程，比如goroutine，而非对称协程只能切换到调用他的调度器。这里实现的是一个非对称协程。

## coroutine源码分析
详细的注释代码在[coroutine源码注释](https://github.com/Nickqiaoo/coroutine)
我们先来看一下头文件
```c
#define COROUTINE_DEAD 0  //协程状态
#define COROUTINE_READY 1
#define COROUTINE_RUNNING 2
#define COROUTINE_SUSPEND 3

struct schedule; //协程调度器

typedef void (*coroutine_func)(struct schedule *, void *ud); //协程执行函数

struct schedule * coroutine_open(void); //创建协程调度器
void coroutine_close(struct schedule *); //关闭协程调度器

int coroutine_new(struct schedule *, coroutine_func, void *ud); //用协程调度器创建一个协程
void coroutine_resume(struct schedule *, int id); //恢复id号协程
int coroutine_status(struct schedule *, int id); //返回id号协程的状态
int coroutine_running(struct schedule *); //返回正在执行的协程id
void coroutine_yield(struct schedule *); //保存上下文后中断当前协程执行
```
整个实现就这么几个函数。用法就是先使用coroutine_open创建协程调度器，然后coroutine_new协程，在协程中使用coroutine_yield中断执行，同一个线程中使用coroutine_resume恢复协程执行。

接下来我们看具体的实现。

由于协程切换时需要保存当前上下文环境，这里用到了**ucontext**这个库，它有四个函数。
```c
typedef struct ucontext {   //上下文结构体
    struct ucontext *uc_link;  // 该上下文执行完时要恢复的上下文
    sigset_t         uc_sigmask;  
    stack_t          uc_stack;  //使用的栈
    mcontext_t       uc_mcontext;  
    ...  
} ucontext_t;  
int getcontext(ucontext_t *ucp); //将当前上下文保存到ucp
int setcontext(const ucontext_t *ucp); //切换到上下文ucp
void makecontext(ucontext_t *ucp, void (*func)(), int argc, ...); //修改上下文入口函数
int swapcontext(ucontext_t *oucp, ucontext_t *ucp); //保存当前上下文到oucp，切换到上下文ucp
```
源码中比较简单的函数就不说了，重点说几个难理解的地方。
```c
void 
coroutine_resume(struct schedule * S, int id) {
	assert(S->running == -1);
	assert(id >=0 && id < S->cap);
	struct coroutine *C = S->co[id];
	if (C == NULL)
		return;
	int status = C->status;
	switch(status) {
	case COROUTINE_READY: //如果状态是ready也就是第一次创建
		getcontext(&C->ctx); //获取当前上下文
		C->ctx.uc_stack.ss_sp = S->stack; //将协程栈设置为调度器的共享栈
		C->ctx.uc_stack.ss_size = STACK_SIZE; //设置栈容量  使用时栈顶栈底同时指向S->stack+STACK_SIZE，栈顶向下扩张
		C->ctx.uc_link = &S->main; //将返回上下文设置为调度器的上下文，协程执行完后会返回到main上下文
		S->running = id; //设置调度器当前运行的协程id
		C->status = COROUTINE_RUNNING; //设置协程状态
		uintptr_t ptr = (uintptr_t)S;
		makecontext(&C->ctx, (void (*)(void)) mainfunc, 2, (uint32_t)ptr, (uint32_t)(ptr>>32));//重置上下文执行mainfunc
		swapcontext(&S->main, &C->ctx); //保存当前上下文到main，跳转到ctx的上下文
		break;
	case COROUTINE_SUSPEND: //如果状态时暂停也就是之前yield过
		memcpy(S->stack + STACK_SIZE - C->size, C->stack, C->size); //将协程之前保存的栈拷贝到调度器的共享栈
		S->running = id;
		C->status = COROUTINE_RUNNING;
		swapcontext(&S->main, &C->ctx); //同上
		break;
	default:
		assert(0);
	}
}
```
resume函数回复协程的运行，协程运行时使用共享栈，中断时将栈保存到私有栈中。

所谓的共享栈，就是将协程所使用的栈设为调度器的栈，由于S->stack为低地址，S->stack+STACK_SIZE为高地址，使用时，栈顶栈底同时指向S->stack+STACK_SIZE，栈顶向低地址扩张。当yield时将已使用的栈拷贝到协程的私有栈当中，resume时将私有栈拷贝到S->stack中。

这样做的好处是一个协程可以使用的栈空间很大，而且不会有提前分配导致的空间过大和浪费，只是拷贝对性能略有些影响。
```c
C->ctx.uc_stack.ss_sp = S->stack; //将协程栈设置为调度器的共享栈
C->ctx.uc_stack.ss_size = STACK_SIZE; //设置栈容量,使用时栈顶栈底同时指向S->stack+STACK_SIZE，栈顶向下扩张
```
这两行将协程栈设置为共享栈。
```c
static void
_save_stack(struct coroutine *C, char *top) { //top为栈底
	char dummy = 0; //这里定义一个char变量，dummy地址为栈顶
	assert(top - &dummy <= STACK_SIZE); //dummy地址减栈底地址为当前使用的栈大小
	if (C->cap < top - &dummy) { //如果当前协程栈大小小于已用大小，重新分配
		free(C->stack);
		C->cap = top-&dummy;
		C->stack = malloc(C->cap);
	}
	C->size = top - &dummy;
	memcpy(C->stack, &dummy, C->size); //将共享栈拷贝到协程栈
}
```
这是保存栈的函数，这里用了一个很巧妙的方法，定义了一个局部变量dummy，此时dummy的地址应该是栈顶，而top是栈底，这样我们就知道当前协程使用的栈的大小。注意memcpy是从低地址开始拷贝的。

剩下的代码就很简单了，我在github上对源码进行了详细的注释，只要理解了保存栈的过程应该就没什么难度了。